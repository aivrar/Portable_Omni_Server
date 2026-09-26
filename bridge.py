"""Reverse-proxy bridge for Omni Studio.

The template C launcher navigates the WebView to this bridge.
All requests are proxied to the API gateway on 127.0.0.1:8200.
"""

import atexit
import base64
import hmac
import http.server
import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import select
import socket
from datetime import datetime, timezone
from pathlib import Path

PORT = int(os.environ.get("BRIDGE_PORT", 9200))
API_PORT = int(os.environ.get("API_PORT", 8200))
API_URL = f"http://127.0.0.1:{API_PORT}"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Bind loopback by default — this matches the documented trust boundary
# (app_intents #13/#24). The WebView reaches the bridge/gateway through WSL2
# localhost-forwarding, which dials 127.0.0.1 *inside* the VM (the only way to
# reach a loopback-bound socket), so a 127.0.0.1 bind keeps the app reachable
# while removing the LAN / sibling-distro exposure of a 0.0.0.0 bind. Operators
# who genuinely need the bridge on the LAN opt in explicitly with
# OMNI_API_ALLOW_REMOTE=1 (or by setting OMNI_BRIDGE_HOST / OMNI_API_HOST to a
# specific interface).
_ALLOW_REMOTE = _env_flag("OMNI_API_ALLOW_REMOTE")
_DEFAULT_BIND = "0.0.0.0" if _ALLOW_REMOTE else "127.0.0.1"

# The gateway is only ever reached from the bridge over loopback (API_URL is
# 127.0.0.1), so it never needs a wider bind on the bridge's account; widen
# only on the same explicit opt-in.
API_BIND_HOST = os.environ.get("OMNI_API_HOST") or _DEFAULT_BIND

try:
    IN_WSL = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or "microsoft" in os.uname().release.lower()
except AttributeError:
    IN_WSL = False
BIND_ADDR = os.environ.get("OMNI_BRIDGE_HOST") or os.environ.get("BRIDGE_HOST") or _DEFAULT_BIND

OPT_DIR = "/opt/omni_studio"
LOG_DIR = os.environ.get("OMNI_LOG_DIR", f"{OPT_DIR}/output/logs")
RUNTIME_DIR = os.environ.get("OMNI_RUNTIME_DIR", f"{OPT_DIR}/cache/runtime")
LOG_MAX_BYTES = int(os.environ.get("OMNI_BRIDGE_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
# Cap proxy request bodies at the gateway's asset-upload limit (default 30 GiB)
# plus a margin for multipart/encoding overhead.
ASSET_UPLOAD_MAX_GB = float(os.environ.get("OMNI_ASSET_UPLOAD_MAX_GB", "30"))
MAX_BODY = int(ASSET_UPLOAD_MAX_GB * 1024 ** 3) + (256 * 1024 * 1024)
# ACE-Step's typed gateway contract allows 1,800-second synchronous inference.
# Give the reverse proxy a small transport margin so it never abandons a
# still-valid request while the gateway and worker continue running.
API_PROXY_TIMEOUT_SECONDS = max(
    60.0, float(os.environ.get("OMNI_API_PROXY_TIMEOUT_SECONDS", "1860"))
)
CPU_LIMIT = max(1, int(os.environ.get("OMNI_CPU_LIMIT", "6")))
MEMORY_LIMIT_GB = max(1, int(os.environ.get("OMNI_MEMORY_LIMIT_GB", "24")))
SWAP_LIMIT_GB = max(0, int(os.environ.get("OMNI_SWAP_LIMIT_GB", "2")))
GATEWAY_MEMORY_RESERVE_GB = max(
    1, int(os.environ.get("OMNI_GATEWAY_MEMORY_RESERVE_GB", "3"))
)
PID_LIMIT = max(64, int(os.environ.get("OMNI_PID_LIMIT", "512")))
_LOOPBACK_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\]):[0-9]+$")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
except Exception:
    LOG_DIR = "/tmp"
    RUNTIME_DIR = "/tmp"

LOG_FILE = os.path.join(RUNTIME_DIR, "bridge_debug.log")
API_LOG_FILE = os.path.join(LOG_DIR, "omni_studio_output.log")
GATEWAY_PID_FILE = os.path.join(RUNTIME_DIR, "gateway.pid")
API_TOKEN_FILE = os.path.join(RUNTIME_DIR, "api_token")
VENV_PY = f"{OPT_DIR}/venv/bin/python3"
SHUTDOWN_SCRIPT = f"{OPT_DIR}/server/omni_shutdown.py"
_log_fh = None
_log_lock = threading.Lock()
_windows_relay_process = None
_windows_relay_ingress = None
_windows_relay_lease = None
_windows_relay_status = None
_windows_relay_stop = threading.Event()


def _rotate_if_large(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except Exception:
        pass


def log(msg):
    global _log_fh
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            if _log_fh is None:
                _rotate_if_large(LOG_FILE)
                _log_fh = open(LOG_FILE, "a")
            _log_fh.write(line + "\n")
            _log_fh.flush()
        except Exception:
            pass


def _close_log():
    global _log_fh
    with _log_lock:
        if _log_fh:
            try:
                _log_fh.close()
            except Exception:
                pass
            _log_fh = None


def _write_control(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as control:
        control.write(value)


def _apply_resource_limits() -> None:
    """Contain the bridge and every child process in app-owned cgroups.

    WSL2 applies host-wide VM limits, so this process-level boundary prevents
    Omni workers from consuming the entire shared VM. Children inherit both
    cgroup membership and CPU affinity. All values remain operator-configurable.
    """
    cpu_count = os.cpu_count() or 1
    allowed_cpus = set(range(min(CPU_LIMIT, cpu_count)))
    try:
        os.sched_setaffinity(0, allowed_cpus)
    except (AttributeError, OSError) as exc:
        log(f"CPU affinity limit unavailable: {exc}")
    try:
        os.nice(5)
    except OSError as exc:
        log(f"process priority adjustment unavailable: {exc}")

    controls = (
        ("cpu", {"cpu.cfs_period_us": "100000", "cpu.cfs_quota_us": str(CPU_LIMIT * 100000)}),
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
                _write_control(os.path.join(group, filename), value)
            _write_control(os.path.join(group, "tasks"), str(os.getpid()))
            if controller == "memory" and MEMORY_LIMIT_GB > 1:
                # Keep heavyweight model processes below the app-wide ceiling
                # so the bridge/gateway retains enough headroom to report and
                # stop a failing workload. Comfy and model workers are moved
                # into this child immediately after spawn by the gateway.
                workload_gb = max(
                    1,
                    MEMORY_LIMIT_GB
                    - min(GATEWAY_MEMORY_RESERVE_GB, MEMORY_LIMIT_GB - 1),
                )
                workload_group = os.path.join(group, "workloads")
                os.makedirs(workload_group, exist_ok=True)
                _write_control(
                    os.path.join(workload_group, "memory.limit_in_bytes"),
                    str(workload_gb * 1024 ** 3),
                )
                _write_control(
                    os.path.join(workload_group, "memory.memsw.limit_in_bytes"),
                    str((workload_gb + SWAP_LIMIT_GB) * 1024 ** 3),
                )
                _write_control(
                    os.path.join(workload_group, "memory.swappiness"), "10"
                )
            applied.append(controller)
        except OSError as exc:
            log(f"{controller} cgroup limit unavailable: {exc}")
    log(
        "Resource boundary: "
        f"cpu={min(CPU_LIMIT, cpu_count)}/{cpu_count}, "
        f"memory={MEMORY_LIMIT_GB}GiB+{SWAP_LIMIT_GB}GiB swap, pids={PID_LIMIT}, "
        f"gateway_reserve={min(GATEWAY_MEMORY_RESERVE_GB, max(0, MEMORY_LIMIT_GB - 1))}GiB, "
        f"cgroups={','.join(applied) or 'none'}"
    )


def _check_api_alive() -> bool:
    try:
        r = urllib.request.urlopen(f"{API_URL}/health", timeout=2)
        try:
            return r.status == 200
        finally:
            r.close()
    except Exception:
        return False


def _local_network_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
    except OSError:
        return ""
    if not host or host.startswith("127."):
        return ""
    return host


def _is_loopback_bind(host: str) -> bool:
    clean = (host or "").strip().lower().strip("[]")
    return clean in {"127.0.0.1", "localhost", "::1"}


class _WindowsIngressHandler(socketserver.BaseRequestHandler):
    """Forward the private WSL interface back into the loopback-only bridge."""

    def handle(self):
        # Only the locally launched relay knows this per-launch capability.
        # A private WSL address alone is not an authentication boundary.
        expected = self.server.relay_secret
        self.request.settimeout(5)
        supplied = b""
        try:
            while len(supplied) < len(expected):
                chunk = self.request.recv(len(expected) - len(supplied))
                if not chunk:
                    return
                supplied += chunk
        except OSError:
            return
        if not hmac.compare_digest(supplied, expected):
            return
        self.request.settimeout(None)
        upstream = socket.create_connection(("127.0.0.1", PORT), timeout=10)
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


class _WindowsIngressServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _windows_path(path: Path) -> str:
    result = subprocess.run(
        ["wslpath", "-w", str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"could not convert WSL path for Windows: {path}")
    return result.stdout.strip()


def _find_windows_pythonw() -> Path | None:
    # The launcher supplies its current directory on every launch, including
    # after the portable folder has moved. Prefer our embedded runtime so a
    # packaged app does not depend on a separate Windows Python installation.
    app_dir = os.environ.get("TQ_APP_DIR", "").strip()
    if app_dir:
        bundled = Path(app_dir) / "runtime" / "python" / "pythonw.exe"
        if bundled.is_file():
            return bundled
    candidates = []
    try:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "where", "pythonw.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for raw in result.stdout.splitlines():
                value = raw.strip().rstrip("\r")
                if len(value) >= 3 and value[1:3] == ":\\":
                    drive = value[0].lower()
                    candidates.append(Path(f"/mnt/{drive}/{value[3:].replace(chr(92), '/') }"))
    except Exception:
        pass
    candidates.extend(
        sorted(
            Path("/mnt/c/Users").glob(
                "*/AppData/Local/Programs/Python/Python*/pythonw.exe"
            ),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _relay_heartbeat(path: Path) -> None:
    while not _windows_relay_stop.wait(2):
        try:
            path.touch()
        except OSError:
            return


def _start_windows_loopback_relay() -> bool:
    """Expose the WSL loopback bridge on Windows loopback without WSL NAT magic."""
    global _windows_relay_process, _windows_relay_ingress
    global _windows_relay_lease, _windows_relay_status

    if not IN_WSL or not _is_loopback_bind(BIND_ADDR):
        return False
    if _env_flag("OMNI_DISABLE_WINDOWS_LOOPBACK_RELAY"):
        log("Windows loopback relay disabled by environment")
        return False

    wsl_host = _local_network_host()
    pythonw = _find_windows_pythonw()
    helper = Path(__file__).resolve().with_name("windows_loopback_relay.py")
    if not wsl_host or pythonw is None or not helper.is_file():
        log(
            "Windows loopback relay unavailable: "
            f"wsl_host={wsl_host or 'missing'} "
            f"pythonw={'ok' if pythonw else 'missing'} "
            f"helper={'ok' if helper.is_file() else 'missing'}"
        )
        return False

    ingress_port = int(os.environ.get("OMNI_WINDOWS_RELAY_PORT", str(PORT + 10000)))
    relay_dir = _registry_dir().parent / "runtime"
    relay_dir.mkdir(parents=True, exist_ok=True)
    lease = relay_dir / f"omni_studio_relay_{PORT}_{os.getpid()}.lease"
    status = relay_dir / f"omni_studio_relay_{PORT}_{os.getpid()}.json"
    relay_secret = os.urandom(32).hex().encode("ascii")
    lease_fd = os.open(str(lease), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(lease_fd, "wb") as lease_file:
        lease_file.write(relay_secret)

    try:
        ingress = _WindowsIngressServer((wsl_host, ingress_port), _WindowsIngressHandler)
        ingress.relay_secret = relay_secret
    except OSError as exc:
        log(f"Windows loopback relay ingress failed on {wsl_host}:{ingress_port}: {exc}")
        try:
            lease.unlink()
        except OSError:
            pass
        return False
    threading.Thread(
        target=ingress.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="omni-windows-relay-ingress",
        daemon=True,
    ).start()

    try:
        command = [
            str(pythonw),
            _windows_path(helper),
            "--listen-port", str(PORT),
            "--target-host", wsl_host,
            "--target-port", str(ingress_port),
            "--lease", _windows_path(lease),
            "--status", _windows_path(status),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        ingress.shutdown()
        ingress.server_close()
        try:
            lease.unlink()
        except OSError:
            pass
        log(f"Windows loopback relay launch failed: {exc}")
        return False

    _windows_relay_process = process
    _windows_relay_ingress = ingress
    _windows_relay_lease = lease
    _windows_relay_status = status
    _windows_relay_stop.clear()
    threading.Thread(
        target=_relay_heartbeat,
        args=(lease,),
        name="omni-windows-relay-lease",
        daemon=True,
    ).start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            payload = json.loads(status.read_text(encoding="utf-8"))
            if payload.get("ready"):
                log(
                    "Windows loopback relay ready: "
                    f"127.0.0.1:{PORT} -> {wsl_host}:{ingress_port} -> 127.0.0.1:{PORT}"
                )
                return True
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(0.25)

    log("Windows loopback relay did not become ready")
    _stop_windows_loopback_relay()
    return False


def _stop_windows_loopback_relay() -> None:
    global _windows_relay_process, _windows_relay_ingress
    global _windows_relay_lease, _windows_relay_status

    _windows_relay_stop.set()
    lease, status = _windows_relay_lease, _windows_relay_status
    _windows_relay_lease = None
    _windows_relay_status = None
    for path in (lease, status):
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
    if _windows_relay_ingress is not None:
        try:
            _windows_relay_ingress.shutdown()
            _windows_relay_ingress.server_close()
        except Exception:
            pass
        _windows_relay_ingress = None
    if _windows_relay_process is not None:
        try:
            if _windows_relay_process.poll() is None:
                _windows_relay_process.terminate()
                _windows_relay_process.wait(timeout=3)
        except Exception:
            pass
        _windows_relay_process = None


def _windows_localappdata_from_wsl():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo %LOCALAPPDATA%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            win_path = result.stdout.strip().rstrip("\r\n")
            if win_path and win_path != "%LOCALAPPDATA%" and len(win_path) >= 2 and win_path[1] == ":":
                drive = win_path[0].lower()
                rest = win_path[2:].replace("\\", "/").lstrip("/")
                path = Path(f"/mnt/{drive}/{rest}")
                if path.exists():
                    return path
    except Exception:
        pass
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username:
        path = Path("/mnt/c/Users") / username / "AppData" / "Local"
        if path.exists():
            return path
    return None


def _registry_dir() -> Path:
    local = _windows_localappdata_from_wsl()
    if local is not None:
        path = local / "AppHub" / "registry"
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(xdg) / "AppHub" / "registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_DISCOVERY_PUBLISHED = False


def _api_session() -> dict:
    try:
        with urllib.request.urlopen(f"{API_URL}/api/session", timeout=3) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _publish_discovery() -> None:
    global _DISCOVERY_PUBLISHED
    endpoints = {
        "api": f"http://127.0.0.1:{PORT}",
        "web": f"http://127.0.0.1:{PORT}/",
        "gateway": f"http://127.0.0.1:{API_PORT}",
    }
    wsl_host = _local_network_host()
    if wsl_host and not _is_loopback_bind(BIND_ADDR):
        endpoints.update({
            "wsl_api": f"http://{wsl_host}:{PORT}",
            "wsl_web": f"http://{wsl_host}:{PORT}/",
            "wsl_gateway": f"http://{wsl_host}:{API_PORT}",
        })
    auth = {
        "header": "X-Omni-Token",
        "session_endpoint": endpoints["api"].rstrip("/") + "/api/session",
    }
    payload = {
        "name": "omni_studio",
        "version": "1.0.0",
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_dir": str(Path(__file__).resolve().parent),
        "endpoints": endpoints,
        "auth": auth,
        "extra": {
            "capabilities": ["image", "video", "audio", "media", "generation", "comfyui"],
            "api_port": API_PORT,
            "bridge_port": PORT,
            "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        },
    }
    path = _registry_dir() / "omni_studio.json"
    _atomic_write_json(path, payload)
    # Discovery exposes only loopback endpoints and the session bootstrap URL;
    # the per-instance secret stays inside the distro process boundary.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _DISCOVERY_PUBLISHED = True
    log(f"discovery registry published at {path}")


def _unpublish_discovery() -> None:
    if not _DISCOVERY_PUBLISHED:
        return
    try:
        path = _registry_dir() / "omni_studio.json"
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
            if int(current.get("pid") or -1) == os.getpid():
                path.unlink()
    except OSError as e:
        log(f"could not unpublish discovery registry: {e}")


class _LengthBoundedReader:
    """File-like wrapper yielding at most ``limit`` bytes from ``src``.

    http.client streams a file-like request body by calling ``read()`` until it
    returns ``b""``. ``self.rfile`` is an unbounded reader over the client
    socket, so passing it directly would make http.client block reading past
    the request body, waiting for a socket EOF that a keep-alive client never
    sends. This caps the body at exactly ``Content-Length`` bytes.
    """

    def __init__(self, src, limit):
        self._src = src
        self._remaining = max(0, int(limit))

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self._remaining
        else:
            size = min(size, self._remaining)
        data = self._src.read(size)
        self._remaining -= len(data)
        return data


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    _FORWARDED_REQUEST_HEADERS = (
        "Content-Type", "Accept", "Authorization", "X-Omni-Token", "Cookie",
        "Range", "If-Range",
    )
    _SKIPPED_RESPONSE_HEADERS = frozenset({
        "transfer-encoding", "connection", "access-control-allow-origin",
        "content-encoding", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "upgrade",
    })
    _MAX_BODY = MAX_BODY
    _STREAM_THRESHOLD = 8 * 1024 * 1024
    _ALLOWED_PREFIXES = ("/api/", "/v1/", "/health", "/metrics", "/static/", "/docs", "/openapi.json")

    def _allowed_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        host = (self.headers.get("Host") or "").lower()
        parsed = urllib.parse.urlparse(origin)
        if ((_ALLOW_REMOTE or _LOOPBACK_ORIGIN_RE.fullmatch(origin))
                and parsed.scheme in {"http", "https"} and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in {"", "/"} and parsed.netloc.lower() == host):
            return origin
        return False

    def _send_cors_headers(self):
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/live":
            self._send_json(200, {
                "ok": True,
                "service": "omni_studio_bridge",
                "api_server": "connected" if _check_api_alive() else "starting",
            })
            return
        if self.path == "/api/status":
            alive = _check_api_alive()
            self._send_json(200, {
                "status": "ok" if alive else "degraded",
                "api_server": "connected" if alive else "unreachable",
            })
            return
        self._proxy()

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/app/shutdown":
            self._handle_app_shutdown()
            return
        self._proxy()

    def _handle_app_shutdown(self):
        """Authenticate locally, acknowledge, then stop the whole app tree."""
        if self._allowed_origin() is False:
            self._send_json(403, {"error": "Forbidden origin"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length < 0 or content_length > 4096:
            self._send_json(413, {"error": "Request body too large"})
            return
        try:
            payload = json.loads(
                self.rfile.read(content_length).decode("utf-8")
            ) if content_length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON"})
            return
        if payload.get("confirm") is not True:
            self._send_json(400, {"error": "Set confirm=true to proceed"})
            return
        expected = _read_runtime_api_token()
        provided = self.headers.get("X-Omni-Token")
        if not expected or not _token_matches(provided, expected):
            self._send_json(401, {"error": "Invalid or missing token"})
            return

        log("Full application shutdown requested from UI")
        self._send_json(202, {
            "status": "shutting_down",
            "scope": "workers, comfyui, jobs, gateway, bridge, watchdog",
        })
        threading.Thread(
            target=_begin_full_app_shutdown,
            args=(self.server,),
            name="omni-full-shutdown",
            daemon=True,
        ).start()

    def do_DELETE(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def do_OPTIONS(self):
        if self._allowed_origin() is False:
            self._send_json(403, {"error": "Forbidden origin"})
            return
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Omni-Token")
        self.end_headers()

    def _proxy(self):
        if self._allowed_origin() is False:
            self._send_json(403, {"error": "Forbidden origin"})
            return

        decoded_path = urllib.parse.unquote(self.path)
        if (".." in self.path or "\x00" in self.path
                or ".." in decoded_path or "\x00" in decoded_path):
            self._send_json(403, {"error": "Invalid path"})
            return

        path_only = self.path.split("?", 1)[0]
        if urllib.parse.unquote(path_only) == "/api/session" and not _is_loopback_bind(self.client_address[0]):
            self._send_json(403, {"error": "Session bootstrap is local only"})
            return
        if path_only != "/" and not any(path_only.startswith(p) for p in self._ALLOWED_PREFIXES):
            self._send_json(403, {"error": "Forbidden path"})
            return

        # WebSocket upgrade detection — phase 9.
        upgrade = (self.headers.get("Upgrade") or "").lower()
        connection = (self.headers.get("Connection") or "").lower()
        if upgrade == "websocket" and "upgrade" in connection:
            self._proxy_websocket()
            return

        target_url = f"{API_URL}{self.path}"

        try:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self._send_json(411, {"error": "Send a Content-Length; chunked requests are not supported"})
                return
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length < 0 or content_length > self._MAX_BODY:
                self._send_json(413, {"error": "Request body too large"})
                return

            # Stream large bodies straight from the socket instead of buffering
            # them in RAM. http.client chunks a file-like body when an explicit
            # Content-Length header is set. Small bodies are read into memory.
            if content_length > self._STREAM_THRESHOLD:
                req = urllib.request.Request(
                    target_url,
                    data=_LengthBoundedReader(self.rfile, content_length),
                    method=self.command,
                )
                req.add_header("Content-Length", str(content_length))
            else:
                body = self.rfile.read(content_length) if content_length > 0 else None
                req = urllib.request.Request(target_url, data=body, method=self.command)
            # Media elements use byte ranges to probe MP4 metadata and seek.
            # Preserve Range/If-Range through the Windows bridge; without them
            # the upstream correctly advertises Accept-Ranges but every browser
            # request is downgraded to a full 200 response and video playback
            # can remain stuck on the poster frame.
            for header in self._FORWARDED_REQUEST_HEADERS:
                val = self.headers.get(header)
                if val:
                    req.add_header(header, val)
            last_event_id = self.headers.get("Last-Event-ID")
            if last_event_id:
                req.add_header("Last-Event-ID", last_event_id)

            resp = urllib.request.urlopen(req, timeout=API_PROXY_TIMEOUT_SECONDS)
            if (
                self.command == "POST"
                and path_only in {"/api/system/restart", "/api/restart"}
                and 200 <= resp.status < 300
            ):
                _note_gateway_restart_requested()
            # Stream Server-Sent Events line-by-line based on the upstream
            # response type (covers chat token streaming and log streams).
            is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "").lower()
            try:
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() not in self._SKIPPED_RESPONSE_HEADERS:
                        self.send_header(key, val)
                self._send_cors_headers()
                self.end_headers()

                if is_sse:
                    try:
                        while True:
                            line = resp.readline()
                            if not line:
                                break
                            self.wfile.write(line)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                resp.close()

        except urllib.error.HTTPError as e:
            try:
                body = e.read()
                e.close()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        except Exception as e:
            log(f"Proxy error: {path_only} -> {type(e).__name__}")
            try:
                if self.command == "GET" and self.path.split("?", 1)[0] in {"/", "/index.html"}:
                    body = b"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Starting Omni Studio</title>
<style>body{margin:0;background:#101318;color:#eef2f7;font:16px system-ui;display:grid;place-items:center;min-height:100vh}main{text-align:center;padding:28px}p{color:#aeb8c6}.dot{display:inline-block;animation:p 1s infinite alternate}@keyframes p{to{opacity:.25}}</style>
</head><body><main><h1>Starting Omni Studio<span class=\"dot\">...</span></h1>
<p>The local API is initializing. This window will reconnect automatically.</p></main>
<script>setTimeout(function(){location.reload()},1000)</script></body></html>"""
                    self.send_response(503)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Retry-After", "1")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "API server unreachable",
                }).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---- WebSocket tunnel -----------------------------------------------
    def _proxy_websocket(self):
        """Forward a WebSocket upgrade to the API gateway as a transparent TCP tunnel.

        We don't speak WebSocket framing here - once the upstream has
        responded with 101 Switching Protocols, we just pump bytes in both
        directions via select(). The gateway speaks WS framing on both ends.
        """
        client_sock = self.connection
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", API_PORT), timeout=10)
            upstream.settimeout(None)

            request_lines = [f"{self.command} {self.path} HTTP/1.1"]
            for k, v in self.headers.items():
                # Header forwarding rule for WS handshakes:
                #   * keep Host (real upstream is loopback, but FastAPI uses it
                #     for routing; rewrite to the upstream port).
                #   * drop CORS-related headers we'd otherwise re-emit.
                request_lines.append(f"{k}: {v}")
            request_lines.append("")
            request_lines.append("")
            handshake = "\r\n".join(request_lines).encode("latin-1")
            upstream.sendall(handshake)

            # Tunnel until either side closes.
            sockets = [client_sock, upstream]
            # Close abandoned tunnels: bail after ~300s (5 x 60s) with no traffic.
            idle_selects = 0
            while True:
                try:
                    rlist, _, xlist = select.select(sockets, [], sockets, 60)
                except (OSError, ValueError):
                    break
                if xlist:
                    break
                if not rlist:
                    idle_selects += 1
                    if idle_selects >= 5:
                        break
                    continue
                idle_selects = 0
                for s in rlist:
                    try:
                        data = s.recv(65536)
                    except (OSError, ConnectionResetError):
                        return
                    if not data:
                        return
                    other = upstream if s is client_sock else client_sock
                    try:
                        other.sendall(data)
                    except (OSError, BrokenPipeError, ConnectionResetError):
                        return
        except (OSError, ConnectionRefusedError) as e:
            log(f"WS upstream connect failed: {e}")
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "WS upstream unreachable"}).encode())
            except Exception:
                pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    def log_message(self, format, *args):
        pass


# ---- Diagnostics ----
def diagnose():
    log("=" * 60)
    log("Omni Studio Bridge — Diagnostics")
    log("=" * 60)
    log(f"Python: {sys.executable} ({sys.version})")
    log(f"CWD: {os.getcwd()}")
    log(f"USER: {os.environ.get('USER', 'unknown')}")

    log("")
    log("--- /opt/omni_studio check ---")
    for path in [
        "/opt/omni_studio", "/opt/omni_studio/env.conf",
        "/opt/omni_studio/venv", "/opt/omni_studio/venv/bin/python3",
        "/opt/omni_studio/comfyui", "/opt/omni_studio/comfyui/main.py",
        "/opt/omni_studio/server", "/opt/omni_studio/server/omni_comfy_server.py",
    ]:
        exists = os.path.exists(path)
        log(f"  {'OK' if exists else 'MISSING'}: {path}")

    log("")
    log("--- GPU ---")
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        log(f"  {r.stdout.strip()}")
    except Exception as e:
        log(f"  {e}")

    log("=" * 60)


# ---- API server management (standalone mode) ----
_api_process = None
_api_log_fh = None
_shutdown_in_progress = False
_shutdown_completed = False
_shutdown_lock = threading.Lock()
_bridge_stopping = threading.Event()
_gateway_restart_pid = None
_gateway_restart_deadline = None
GATEWAY_RESTART_GRACE_SECONDS = max(
    5.0, float(os.environ.get("OMNI_GATEWAY_RESTART_GRACE_SECONDS", "20"))
)


def _read_runtime_api_token() -> str | None:
    try:
        token = Path(API_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= 32 else None


def _token_matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except TypeError:
        return False


def _watchdog_parent_pid() -> int | None:
    """Return the verified watchdog parent, never an unrelated parent PID."""
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return None
    try:
        cmdline = Path(f"/proc/{parent_pid}/cmdline").read_bytes()
    except OSError:
        return None
    command = cmdline.replace(b"\x00", b" ").decode("utf-8", "replace")
    if "bridge_watchdog.py" not in command:
        return None
    app_dir = os.environ.get("TQ_APP_DIR")
    if app_dir and app_dir not in command:
        return None
    return parent_pid


def _windows_host_executable() -> str | None:
    """Resolve this app's Windows executable without accepting path traversal."""
    app_dir = os.environ.get("TQ_APP_DIR", "").rstrip("/")
    match = re.fullmatch(r"/mnt/([A-Za-z])/(.+)", app_dir)
    if not match:
        return None
    parts = match.group(2).split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return f"{match.group(1).upper()}:\\{'\\'.join(parts)}\\Omni_Studio.exe"


def _schedule_windows_host_shutdown(delay_seconds: float = 5.0) -> bool:
    """Close only the matching Omni WebView host after WSL cleanup begins."""
    expected_exe = _windows_host_executable()
    powershell = Path(
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not expected_exe or not powershell.exists():
        return False
    quoted_exe = expected_exe.replace("'", "''")
    script = (
        "$expected='" + quoted_exe + "'; "
        "Start-Sleep -Milliseconds "
        + str(max(0, int(float(delay_seconds) * 1000)))
        + "; "
        + "Get-Process -Name Omni_Studio -ErrorAction SilentlyContinue | "
        + "Where-Object { [string]::Equals($_.Path,$expected," 
        + "[System.StringComparison]::OrdinalIgnoreCase) } | "
        + "Stop-Process -Force"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        subprocess.Popen(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded_script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log(f"Could not schedule Windows host shutdown: {exc}")
        return False
    log(f"Scheduled exact Windows host shutdown for {expected_exe}")
    return True


def _begin_full_app_shutdown(server) -> None:
    """Stop the supervisor so it cannot relaunch the bridge or gateway."""
    time.sleep(0.25)  # let the HTTP acknowledgement reach the WebView
    # Finish the authoritative cleanup before closing the Windows host. Keep
    # WSL alive briefly so the interop-launched helper can start reliably.
    stop_api_server()
    if _schedule_windows_host_shutdown(0):
        time.sleep(3.0)
    watchdog_pid = _watchdog_parent_pid()
    if watchdog_pid is not None:
        log(f"Stopping app watchdog (PID {watchdog_pid})")
        try:
            os.kill(watchdog_pid, signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            log(f"Could not signal watchdog: {exc}")
        else:
            # The watchdog normally signals this bridge immediately. If that
            # contract ever breaks, fall through to the direct cleanup path.
            if _bridge_stopping.wait(3.0):
                return

    log("No responding watchdog; stopping bridge directly")
    stop_api_server()
    try:
        server.shutdown()
    except Exception as exc:
        log(f"Bridge HTTP shutdown failed: {exc}")


def start_api_server():
    global _api_process, _api_log_fh
    if _check_api_alive():
        log("API server already running — skipping subprocess launch")
        return True

    venv_py = "/opt/omni_studio/venv/bin/python3"
    server_script = "/opt/omni_studio/server/omni_comfy_server.py"

    if not os.path.exists(venv_py):
        log(f"ERROR: venv python not found at {venv_py}")
        return False
    if not os.path.exists(server_script):
        log(f"ERROR: server script not found at {server_script}")
        return False

    log(f"Starting API server: {venv_py} {server_script} --host {API_BIND_HOST} --port {API_PORT}")

    try:
        os.makedirs(os.path.dirname(API_LOG_FILE), exist_ok=True)
        _rotate_if_large(API_LOG_FILE)
        _api_log_fh = open(API_LOG_FILE, "a")
    except Exception as e:
        log(f"ERROR: Failed to open API log file: {e}")
        return False
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPYCACHEPREFIX"] = f"{OPT_DIR}/cache/pycache"
    env["TMPDIR"] = f"{OPT_DIR}/cache/tmp"
    env["XDG_CACHE_HOME"] = f"{OPT_DIR}/cache/xdg"
    env["PIP_CACHE_DIR"] = f"{OPT_DIR}/cache/pip"
    env["TORCH_EXTENSIONS_DIR"] = f"{OPT_DIR}/cache/torch_extensions"
    env["TRITON_CACHE_DIR"] = f"{OPT_DIR}/cache/triton"
    env["CUDA_CACHE_PATH"] = f"{OPT_DIR}/cache/cuda"
    env["NUMBA_CACHE_DIR"] = f"{OPT_DIR}/cache/numba"
    env["MPLCONFIGDIR"] = f"{OPT_DIR}/cache/matplotlib"
    env["IMAGEIO_FFMPEG_EXE"] = "/usr/bin/ffmpeg"
    env["OMNI_API_HOST"] = API_BIND_HOST
    env["OMNI_API_PORT"] = str(API_PORT)

    try:
        _api_process = subprocess.Popen(
            [venv_py, server_script, "--host", API_BIND_HOST, "--port", str(API_PORT)],
            stdout=_api_log_fh, stderr=subprocess.STDOUT,
            env=env, cwd="/opt/omni_studio/server",
            start_new_session=True,
        )
    except Exception as e:
        log(f"ERROR: Failed to start API server: {e}")
        _api_log_fh.close()
        _api_log_fh = None
        return False
    log(f"API server launched (PID {_api_process.pid})")
    _write_gateway_pid(_api_process.pid)

    for i in range(60):
        time.sleep(1)
        if _api_process.poll() is not None:
            log(f"API server exited with code {_api_process.returncode}!")
            try:
                with open(API_LOG_FILE) as f:
                    log(f"Server output:\n{f.read()[-2000:]}")
            except Exception:
                pass
            return False
        if _check_api_alive():
            log(f"API server ready after {i+1}s")
            return True
        if i % 10 == 9:
            log(f"Waiting for API server... ({i+1}s)")

    log("API server failed to start within 60s")
    return False


def _supervise_api_once() -> bool:
    """Restart the owned gateway after an intentional restart or crash."""
    global _api_process, _api_log_fh
    global _gateway_restart_pid, _gateway_restart_deadline
    if _bridge_stopping.is_set() or _shutdown_in_progress:
        return False

    proc = _api_process
    if proc is None:
        if _check_api_alive():
            return False
        log("API supervisor found no owned gateway; starting one")
        return bool(start_api_server())

    return_code = proc.poll()
    if return_code is None:
        if (
            _gateway_restart_pid == proc.pid
            and _gateway_restart_deadline is not None
            and time.monotonic() >= _gateway_restart_deadline
        ):
            log(
                "Gateway did not exit within the requested restart grace "
                f"period; forcing PID {proc.pid} down"
            )
            _gateway_restart_pid = None
            _gateway_restart_deadline = None
            _kill_owned_gateway_process(grace=0.0)
            return bool(start_api_server())
        return False

    _gateway_restart_pid = None
    _gateway_restart_deadline = None
    log(f"API server exited with code {return_code}; restarting")
    if _api_log_fh:
        try:
            _api_log_fh.close()
        except Exception:
            pass
        _api_log_fh = None
    _api_process = None
    _remove_gateway_pid()
    if _bridge_stopping.wait(0.5):
        return False
    return bool(start_api_server())


def _note_gateway_restart_requested() -> None:
    """Arm a bounded fallback only after an explicit gateway restart call."""
    global _gateway_restart_pid, _gateway_restart_deadline
    proc = _api_process
    if not proc or proc.poll() is not None:
        return
    _gateway_restart_pid = proc.pid
    _gateway_restart_deadline = time.monotonic() + GATEWAY_RESTART_GRACE_SECONDS


def _monitor_api_server() -> None:
    """Continuously supervise the gateway for the lifetime of the bridge."""
    while not _bridge_stopping.wait(1.0):
        try:
            _supervise_api_once()
        except Exception as exc:
            log(f"API supervisor error: {exc}")


def _write_gateway_pid(pid: int) -> None:
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=RUNTIME_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(pid))
            os.replace(tmp, GATEWAY_PID_FILE)
            from server.process_identity import process_start_time
            fd, tmp = tempfile.mkstemp(dir=RUNTIME_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": pid, "port": API_PORT, "start_time": process_start_time(pid)}, fh)
            os.replace(tmp, GATEWAY_PID_FILE + ".identity.json")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log(f"could not write gateway pid file: {e}")


def _remove_gateway_pid() -> None:
    for path in (GATEWAY_PID_FILE, GATEWAY_PID_FILE + ".identity.json"):
        try:
            os.remove(path)
        except OSError:
            pass


def _run_omni_shutdown_sweep() -> None:
    """Kill gateway, workers, ComfyUI, and any app-owned orphans."""
    if not os.path.exists(VENV_PY) or not os.path.exists(SHUTDOWN_SCRIPT):
        log("shutdown sweep skipped — venv or omni_shutdown.py missing")
        return
    log("Running full Omni shutdown sweep...")
    try:
        result = subprocess.run(
            [VENV_PY, SHUTDOWN_SCRIPT, "--api-url", API_URL],
            capture_output=True,
            text=True,
            timeout=45,
        )
        detail = (result.stdout or result.stderr or "").strip()
        if detail:
            log(f"Shutdown sweep: {detail[:500]}")
        if result.returncode != 0:
            log(f"Shutdown sweep exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        log("Shutdown sweep timed out — sending direct gateway kill")
        _kill_owned_gateway_process(grace=0.0)
    except Exception as e:
        log(f"Shutdown sweep failed: {e}")


def _kill_owned_gateway_process(*, grace: float = 5.0) -> None:
    global _api_process
    from server.process_identity import process_matches, process_start_time, owned_process_group, process_alive
    pid = None
    started = None
    if _api_process and _api_process.poll() is None:
        pid = _api_process.pid
        started = process_start_time(pid)
    elif os.path.exists(GATEWAY_PID_FILE):
        try:
            with open(GATEWAY_PID_FILE, encoding="utf-8") as fh:
                pid = int(fh.read().strip())
            with open(GATEWAY_PID_FILE + ".identity.json", encoding="utf-8") as fh:
                identity = json.load(fh)
            if not isinstance(identity, dict) or identity.get("pid") != pid or identity.get("port") != API_PORT:
                return
            started = identity.get("start_time")
        except (OSError, ValueError):
            pid = None
    if not pid or pid <= 0 or not started:
        return
    expected = Path(OPT_DIR) / "server" / "omni_comfy_server.py"
    if not process_matches(pid, expected, port=API_PORT, start_time=started):
        log("Skipping stale gateway PID record")
        return
    log(f"Stopping owned gateway process (PID {pid})...")
    isolated_group = False
    try:
        pgid = owned_process_group(pid)
        isolated_group = pgid is not None
        if isolated_group:
            os.killpg(pgid, signal.SIGTERM)
        else:
            # Gateways launched before start_new_session=True may share the
            # bridge's process group.  Never signal that shared group.
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            if not process_alive(pid):
                break
        except ProcessLookupError:
            break
        except OSError:
            break
        time.sleep(0.15)
    else:
        if not process_matches(pid, expected, port=API_PORT, start_time=started):
            return
        try:
            if isolated_group:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    if _api_process and _api_process.poll() is None:
        try:
            _api_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _api_process = None
    _remove_gateway_pid()


def stop_api_server():
    global _api_process, _api_log_fh, _shutdown_in_progress, _shutdown_completed
    with _shutdown_lock:
        if _shutdown_in_progress or _shutdown_completed:
            return
        _shutdown_in_progress = True
    _bridge_stopping.set()

    if _api_log_fh:
        try:
            _api_log_fh.close()
        except Exception:
            pass
        _api_log_fh = None

    # Always sweep workers/comfy/gateway — they live outside the bridge process
    # group and may survive even when this bridge did not launch the gateway.
    _run_omni_shutdown_sweep()
    _kill_owned_gateway_process()
    _remove_gateway_pid()

    with _shutdown_lock:
        _shutdown_in_progress = False
        _shutdown_completed = True


def main():
    try:
        os.chdir(OPT_DIR)
    except OSError as exc:
        log(f"could not enter isolated runtime directory {OPT_DIR}: {exc}")
    _apply_resource_limits()
    log(f"Bridge starting: port {PORT} -> proxy to 127.0.0.1:{API_PORT}")
    threading.Thread(target=diagnose, daemon=True).start()
    atexit.register(_close_log)
    atexit.register(stop_api_server)
    atexit.register(_unpublish_discovery)
    atexit.register(_stop_windows_loopback_relay)

    def _sig_handler(signum, frame):
        log(f"Bridge received signal {signum} — cleaning up")
        stop_api_server()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    if not start_api_server():
        log("WARNING: API server failed on first attempt — retrying in 5s...")
        time.sleep(5)
        if not start_api_server():
            log("WARNING: API server failed to start — bridge will proxy but get errors")
    threading.Thread(
        target=_monitor_api_server,
        name="omni-api-supervisor",
        daemon=True,
    ).start()

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = ThreadedHTTPServer((BIND_ADDR, PORT), ProxyHandler)
    log(f"Bridge proxy listening on {BIND_ADDR}:{PORT}")
    _start_windows_loopback_relay()
    try:
        _publish_discovery()
    except Exception as e:
        log(f"could not publish discovery registry: {e}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_windows_loopback_relay()
        stop_api_server()
        server.server_close()


if __name__ == "__main__":
    main()
