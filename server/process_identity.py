"""Read-only Linux process identity checks before using durable PID records."""

from __future__ import annotations

import os
from pathlib import Path


def process_alive(pid: int) -> bool:
    """Probe existence without signals; kill(pid, 0) is not read-only on Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: do not treat as dead
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # fail closed for cleanup callers
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def process_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def process_matches(pid: int, script: str | Path, *, port: int | None = None,
                    start_time: str | None = None) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        argv = [p.decode("utf-8", "surrogateescape") for p in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if p]
        executable = Path(argv[0]).name.lower() if argv else ""
        shell_script = Path(script).suffix == ".sh"
        if not argv or (executable not in {"bash", "sh"} if shell_script else not executable.startswith(("python", "pypy"))):
            return False
        args = argv[1:]
        while args and args[0] in {"-u", "-B", "-O", "-OO", "-s", "-S", "-E", "-I", "-P", "-e", "-x", "-eu"}:
            args = args[1:]
        if not args or Path(args[0]).resolve() != Path(script).resolve():
            return False
        if start_time is not None and process_start_time(pid) != str(start_time):
            return False
        if port is not None:
            index = args.index("--port")
            if args[index + 1] != str(port):
                return False
        return True
    except (OSError, ValueError, IndexError):
        return False


def owned_process_group(pid: int, recorded_pgid: int | None = None) -> int | None:
    """Never signal a shared group or trust an unrelated recorded PGID."""
    try:
        group = os.getpgid(pid)
        if group == pid and group != os.getpgrp() and (recorded_pgid is None or group == recorded_pgid):
            return group
    except (OSError, AttributeError):
        pass
    return None
