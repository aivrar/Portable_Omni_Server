"""HTTP client + token resolver for the Omni Studio CLI.

Token lookup, first hit wins:

  1. ``--token`` CLI flag (passed in via ``Client(token=...)``)
  2. ``OMNI_API_TOKEN`` env var
  3. ``~/.config/omni-studio/token`` (mode 0600 on POSIX)
  4. ``/opt/omni_studio/cache/runtime/api_token`` (works inside WSL)
  5. ``GET <base>/api/session`` (succeeds in loopback-token mode)

The CLI is expected to run either inside the WSL distro that hosts the
gateway, or against a loopback-bound bridge. Step 4 covers the in-WSL case;
step 5 covers the host-side case where the bridge happily returns the token
to a same-origin caller.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = os.environ.get("OMNI_BASE_URL", "http://127.0.0.1:9200")
DEFAULT_TIMEOUT = float(os.environ.get("OMNI_CLI_TIMEOUT_S", "60"))


def _default_timeout() -> httpx.Timeout:
    """Structured timeout: snappy connect/pool, generous read/write so long
    generation POSTs don't trip a uniform 60s deadline."""
    long = max(DEFAULT_TIMEOUT, 600.0)
    return httpx.Timeout(connect=10.0, read=long, write=long, pool=10.0)


def _user_token_path() -> Path:
    home = Path(os.path.expanduser("~"))
    return home / ".config" / "omni-studio" / "token"


def _runtime_token_path() -> Path:
    return Path("/opt/omni_studio/cache/runtime/api_token")


def _read_secret(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def _is_local_base(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


def resolve_token(explicit: str | None, base_url: str = DEFAULT_BASE_URL, *, refresh: bool = False) -> str:
    """Walk the token-discovery ladder and return a valid bearer token."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("OMNI_API_TOKEN")
    if env and env.strip():
        return env.strip()
    if not _is_local_base(base_url):
        raise RuntimeError("A remote base URL requires --token or OMNI_API_TOKEN explicitly")
    for path in ((_runtime_token_path(),) if refresh else (_runtime_token_path(), _user_token_path())):
        v = _read_secret(path)
        if v:
            return v
    # Last resort: ask the bridge over loopback.
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{base_url.rstrip('/')}/api/session")
            if r.status_code == 200:
                t = (r.json() or {}).get("token")
                if t:
                    return t
    except httpx.HTTPError:
        pass
    raise RuntimeError(
        "Could not resolve API token. Pass --token, set OMNI_API_TOKEN, "
        "or run from inside the Omni Studio WSL distro."
    )


def cache_user_token(token: str) -> None:
    """Persist a token under ``~/.config/omni-studio/token`` (POSIX 0600)."""
    path = _user_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Client:
    """Thin httpx wrapper that injects the X-Omni-Token header on every call."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 token: str | None = None,
                 timeout: httpx.Timeout | float | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = resolve_token(token, base_url=self.base_url)
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else _default_timeout(),
            headers={"X-Omni-Token": self.token},
        )

    def close(self) -> None:
        self._http.close()

    # ---- convenience verbs -------------------------------------------------
    def get(self, path: str, **kw):
        return self._http.get(path, **kw)

    def post(self, path: str, **kw):
        return self._http.post(path, **kw)

    def put(self, path: str, **kw):
        return self._http.put(path, **kw)

    def delete(self, path: str, **kw):
        return self._http.delete(path, **kw)

    @contextlib.contextmanager
    def stream(self, method: str, path: str, **kw):
        # No read timeout for streams: idle gaps between SSE events must not
        # abort a live connection. Keep the connect/pool guards. This is a
        # context manager so the actual request — which httpx fires lazily on
        # __enter__, not when stream() is called — runs inside the try/except.
        kw.setdefault("timeout", httpx.Timeout(connect=10.0, read=None,
                                               write=None, pool=10.0))
        try:
            with self._http.stream(method, path, **kw) as resp:
                yield resp
        except (httpx.TimeoutException, httpx.TransportError) as e:
            print(f"{method} {path} -> {e}", file=sys.stderr)
            raise SystemExit(2)

    # ---- helpers used by every subcommand ----------------------------------
    def call(self, method: str, path: str, *,
             expect: int | tuple[int, ...] = 200,
             **kw) -> dict:
        try:
            r = self._http.request(method, path, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            print(f"{method} {path} -> {e}", file=sys.stderr)
            raise SystemExit(2)
        ok = expect if isinstance(expect, tuple) else (expect,)
        if r.status_code not in ok:
            detail: object = r.text[:300]
            try:
                body = r.json()
                if isinstance(body, dict) and "detail" in body:
                    detail = body["detail"]
                elif body is not None:
                    detail = body
            except (ValueError, json.JSONDecodeError):
                pass
            print(f"[{r.status_code}] {method} {path} -> {detail}", file=sys.stderr)
            sys.exit(2)
        if not r.content:
            return {}
        try:
            return r.json()
        except (ValueError, json.JSONDecodeError):
            return {"raw": r.text}
