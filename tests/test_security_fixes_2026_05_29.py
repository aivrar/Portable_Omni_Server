"""Regression tests for the 2026-05-29 SECURITY fix pass.

Pins the security hardening so it can't silently regress. Scoped to the pure,
Windows-runnable logic; the full-app auth-middleware composition (TES-1) needs
the gateway's runtime deps, so that one integration test skips when the app
can't be imported (it runs on the real WSL/CI box).

  - H1     is_loopback_peer gate for /api/session (the token-dispensing route)
  - H1     security primitives the auth middleware composes (origin/token)
  - TES-1  gateway auth middleware end-to-end (integration; skipped w/o deps)
  - TES-2  bridge loopback-origin regex, proxy path allowlist, bind default
  - TES-5  worker_registry.atomic_pick_and_mark_busy under concurrency
  - TES-6  proxy URL construction + header stripping (SSRF containment)
  - TES-7  hf_tqdm_parser parses untrusted subprocess stdout safely
  - JOB-3  key_store persists api_keys.json 0600 from creation (no umask window)
"""

import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
for _p in (str(ROOT), str(SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# bridge.py creates its log/runtime dirs at import time; redirect them to temp.
_BRIDGE_TMP = tempfile.mkdtemp(prefix="omni_sec_test_")
os.environ.setdefault("OMNI_LOG_DIR", os.path.join(_BRIDGE_TMP, "log"))
os.environ.setdefault("OMNI_RUNTIME_DIR", os.path.join(_BRIDGE_TMP, "rt"))

_POSIX = os.name != "nt"


class LoopbackPeerTests(unittest.TestCase):
    """H1: /api/session is gated on is_loopback_peer when not OMNI_API_ALLOW_REMOTE."""

    def test_loopback_addresses_accepted(self):
        from security import is_loopback_peer
        for ok in ("127.0.0.1", "127.0.0.5", "127.1.2.3", "::1",
                   "::ffff:127.0.0.1", "localhost"):
            self.assertTrue(is_loopback_peer(ok), ok)

    def test_non_loopback_rejected(self):
        from security import is_loopback_peer
        for bad in (None, "", "10.0.0.4", "192.168.1.20", "172.17.0.1",
                    "8.8.8.8", "::2", "2001:db8::1", "0.0.0.0"):
            self.assertFalse(is_loopback_peer(bad), bad)

    def test_bind_loopback_implies_loopback_peer(self):
        # The whole safety argument for the bind change: a connection that
        # reaches a 127.0.0.1 socket necessarily has a loopback peer, so the
        # peer gate never blocks legitimate traffic when bound loopback.
        from security import is_loopback_peer
        self.assertTrue(is_loopback_peer("127.0.0.1"))


class OriginMatchTests(unittest.TestCase):
    """H1: the middleware's Origin 403 path."""

    def test_origin_rules(self):
        from security import origin_matches_host, is_loopback_origin
        # Absent Origin is allowed (non-browser clients); the token still gates.
        self.assertTrue(origin_matches_host(None, "127.0.0.1:8200"))
        self.assertTrue(origin_matches_host("http://127.0.0.1:8200", "127.0.0.1:8200"))
        # Cross-origin (different port / external host) is rejected.
        self.assertFalse(origin_matches_host("http://127.0.0.1:3000", "127.0.0.1:8200"))
        self.assertFalse(origin_matches_host("https://evil.example", "127.0.0.1:8200"))
        self.assertTrue(is_loopback_origin("http://localhost:9200"))
        self.assertFalse(is_loopback_origin("http://192.168.1.5:9200"))


class GatewayAuthMiddlewareTests(unittest.TestCase):
    """TES-1: end-to-end auth middleware. Needs the gateway's runtime deps
    (fastapi/uvicorn/torch-backed routers), so it skips where they're absent."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient  # noqa: F401
            import omni_comfy_server as srv  # noqa: F401
        except Exception as e:  # ImportError or heavy-dep failure
            raise unittest.SkipTest(f"gateway app not importable here: {e}")
        cls.srv = srv
        cls.TestClient = TestClient

    def _client(self):
        # Avoid the lifespan side effects (orphan sweeps, scheduler, discovery)
        # by not entering the TestClient context manager. Present a loopback
        # peer: in production every connection to the gateway's 127.0.0.1 socket
        # is itself loopback (the bridge, or the localhost-forwarded WebView),
        # so the /api/session loopback-peer gate must allow it. The TestClient's
        # default sentinel peer ("testclient") is not loopback, which would
        # otherwise mis-trip the gate.
        try:
            return self.TestClient(self.srv.app, client=("127.0.0.1", 23456))
        except TypeError:  # older Starlette without the client kwarg
            return self.TestClient(self.srv.app)

    def test_protected_route_requires_token(self):
        c = self._client()
        r = c.get("/api/workers")
        self.assertEqual(r.status_code, 401)

    def test_session_route_is_token_exempt_on_loopback(self):
        c = self._client()
        # TestClient peers as 'testclient' / 127.0.0.1; loopback gate allows it.
        r = c.get("/api/session")
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())
        cookie = r.headers.get("set-cookie", "")
        self.assertIn("omni_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_session_cookie_authenticates_browser_requests(self):
        c = self._client()
        self.assertEqual(c.get("/api/session").status_code, 200)
        self.assertEqual(c.get("/api/workers").status_code, 200)

    def test_cross_origin_rejected(self):
        c = self._client()
        r = c.get("/api/session", headers={"origin": "http://evil.example",
                                           "host": "127.0.0.1:8200"})
        self.assertEqual(r.status_code, 403)

    def test_metrics_now_requires_token(self):
        c = self._client()
        self.assertEqual(c.get("/metrics").status_code, 401)

    def test_docs_disabled_by_default(self):
        c = self._client()
        self.assertIn(c.get("/openapi.json").status_code, (404, 403))


class BridgeOriginAndPathTests(unittest.TestCase):
    """TES-2: bridge loopback-origin regex, proxy path allowlist, bind default."""

    def test_loopback_origin_regex(self):
        import bridge
        rx = bridge._LOOPBACK_ORIGIN_RE
        for ok in ("http://127.0.0.1:9200", "https://localhost:8200",
                   "http://[::1]:9200"):
            self.assertTrue(rx.fullmatch(ok), ok)
        for bad in ("http://192.168.0.2:9200", "http://evil.com",
                    "http://127.0.0.1", "ftp://127.0.0.1:9200"):
            self.assertFalse(rx.fullmatch(bad), bad)

    def test_proxy_path_allowlist(self):
        import bridge
        prefixes = bridge.ProxyHandler._ALLOWED_PREFIXES

        def allowed(path_only):
            # Mirror ProxyHandler._proxy's path filter.
            return path_only == "/" or any(path_only.startswith(p) for p in prefixes)

        for ok in ("/", "/api/workers", "/v1/chat/completions", "/metrics",
                   "/static/app.js", "/health"):
            self.assertTrue(allowed(ok), ok)
        for bad in ("/etc/passwd", "/root", "/..", "/admin"):
            self.assertFalse(allowed(bad), bad)

    def test_env_flag_and_default_bind(self):
        import bridge
        # Default (no OMNI_API_ALLOW_REMOTE in this test env) is loopback.
        if not os.environ.get("OMNI_API_ALLOW_REMOTE"):
            self.assertEqual(bridge._DEFAULT_BIND, "127.0.0.1")
            self.assertEqual(bridge.BIND_ADDR, "127.0.0.1")
            self.assertEqual(bridge.API_BIND_HOST, "127.0.0.1")

    def test_env_flag_truthiness(self):
        import bridge
        os.environ["_OMNI_TMP_FLAG"] = "1"
        try:
            self.assertTrue(bridge._env_flag("_OMNI_TMP_FLAG"))
        finally:
            del os.environ["_OMNI_TMP_FLAG"]
        os.environ["_OMNI_TMP_FLAG"] = "off"
        try:
            self.assertFalse(bridge._env_flag("_OMNI_TMP_FLAG"))
        finally:
            del os.environ["_OMNI_TMP_FLAG"]
        self.assertFalse(bridge._env_flag("_OMNI_DEFINITELY_UNSET_FLAG"))


class ProxyContainmentTests(unittest.TestCase):
    """TES-6: ComfyUI proxy URL construction + header stripping (SSRF/leak)."""

    def test_build_target_url_is_host_locked(self):
        from proxy import build_target_url
        self.assertEqual(
            build_target_url("http://127.0.0.1:8188", "prompt"),
            "http://127.0.0.1:8188/prompt")
        # Query passthrough preserved.
        self.assertEqual(
            build_target_url("http://127.0.0.1:8188", "view", "filename=a.png"),
            "http://127.0.0.1:8188/view?filename=a.png")
        # Trailing slash on base is normalized.
        self.assertEqual(
            build_target_url("http://127.0.0.1:8188/", "queue"),
            "http://127.0.0.1:8188/queue")

    def test_request_headers_strip_token_and_cookie_and_host(self):
        from proxy import _filter_request_headers
        out = _filter_request_headers({
            "X-Omni-Token": "secret", "Cookie": "a=b", "Host": "x",
            "Connection": "keep-alive", "Content-Type": "application/json",
        })
        lower = {k.lower() for k in out}
        self.assertNotIn("x-omni-token", lower)
        self.assertNotIn("cookie", lower)
        self.assertNotIn("host", lower)
        self.assertNotIn("connection", lower)  # hop-by-hop
        self.assertIn("content-type", lower)

    def test_subpath_allowlist_rejects_traversal(self):
        from proxy import is_allowed_subpath
        self.assertTrue(is_allowed_subpath("prompt"))
        self.assertTrue(is_allowed_subpath("history/abc"))
        self.assertFalse(is_allowed_subpath("../secret"))
        self.assertFalse(is_allowed_subpath("foo/../bar"))
        self.assertFalse(is_allowed_subpath("admin"))
        self.assertFalse(is_allowed_subpath("a\x00b"))


class WorkerPickConcurrencyTests(unittest.TestCase):
    """TES-5: concurrent atomic_pick_and_mark_busy never double-assigns a worker."""

    def test_no_double_assignment_under_threads(self):
        from worker_registry import WorkerInfo, WorkerRegistry
        registry = WorkerRegistry(9100, 9200)
        n_workers = 12
        for i in range(n_workers):
            registry.register(WorkerInfo(
                worker_id=f"qwen_omni_3b-{i}", model="qwen_omni_3b",
                port=9100 + i, device="cpu", status="ready"))

        picked: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(32)

        def worker(job_i):
            barrier.wait()  # maximize contention
            w = registry.atomic_pick_and_mark_busy("qwen_omni_3b", f"job-{job_i}")
            if w is not None:
                with lock:
                    picked.append(w.worker_id)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly n_workers picks, all distinct: a locking regression would
        # either hand the same worker to two threads or mark more than n busy.
        self.assertEqual(len(picked), n_workers)
        self.assertEqual(len(set(picked)), n_workers)


class HfTqdmParserTests(unittest.TestCase):
    """TES-7: parsing untrusted subprocess stdout must never raise."""

    def test_valid_progress_lines(self):
        from jobs import hf_tqdm_parser
        cur, tot, pct = hf_tqdm_parser(
            "model.bin:  45%|####     | 2.0GB/4.0GB [00:12<00:14, 200MB/s]")
        self.assertEqual(cur, int(2.0 * 1024 ** 3))
        self.assertEqual(tot, int(4.0 * 1024 ** 3))
        self.assertEqual(pct, "model.bin: 45%")
        cur, tot, pct = hf_tqdm_parser("100%|##########| 1.5MB/1.5MB")
        self.assertEqual((cur, tot, pct),
                         (int(1.5 * 1024 ** 2), int(1.5 * 1024 ** 2), "100%"))

    def test_non_progress_and_garbage_return_none_not_raise(self):
        from jobs import hf_tqdm_parser
        for line in ("", "Downloading shards", "not a progress bar",
                     "error: %d things", "50% done"):
            self.assertIsNone(hf_tqdm_parser(line), line)


@unittest.skipUnless(_POSIX, "0600 mode bits are POSIX-only")
class KeyStoreModeTests(unittest.TestCase):
    """JOB-3: api_keys.json is created 0600 with no world-readable umask window."""

    def test_save_is_0600(self):
        from key_store import ApiKeyStore
        with tempfile.TemporaryDirectory() as tmp:
            store = ApiKeyStore(path=Path(tmp) / "api_keys.json")
            key, secret = store.create(scopes=["admin"], label="t")
            self.assertTrue(store.path.exists())
            mode = stat.S_IMODE(os.stat(store.path).st_mode)
            self.assertEqual(mode, 0o600)
            # No stray world-readable temp left behind.
            self.assertFalse((Path(tmp) / "api_keys.json.tmp").exists())
            # Round-trips: a fresh store loads the persisted key.
            reloaded = ApiKeyStore(path=store.path)
            self.assertIsNotNone(reloaded.authenticate(f"{key.id}.{secret}"))


class AudioLoaderContainmentTests(unittest.TestCase):
    """AUD-1: user variant/custom names can't escape the model root.

    The loader modules defer torch to inside functions, so the pure path
    helpers import cleanly; guarded anyway in case a dep is missing here."""

    @classmethod
    def setUpClass(cls):
        try:
            import ace_step_loaders as ace
            import audio_lab_loaders as al
        except Exception as e:
            raise unittest.SkipTest(f"audio loaders not importable here: {e}")
        cls.ace = ace
        cls.al = al

    def test_validate_name_segment_rejects_traversal(self):
        for mod in (self.ace, self.al):
            self.assertEqual(mod._validate_name_segment("good-name_1.2", "model"),
                             "good-name_1.2")
            for bad in ("", "..", "../etc", "a/b", "a\\b", ".hidden",
                        "with\x00nul", "/abs", "a/../b"):
                with self.assertRaises(ValueError, msg=f"{mod.__name__}:{bad!r}"):
                    mod._validate_name_segment(bad, "model")

    def test_ensure_within_root_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "sub").mkdir()
            for mod in (self.ace, self.al):
                # A contained path resolves and is returned.
                inside = mod._ensure_within_root(root / "sub", root, "model")
                self.assertEqual(inside, (root / "sub").resolve())
                # A traversal that climbs out of the root is rejected.
                with self.assertRaises(ValueError):
                    mod._ensure_within_root(root / ".." / "evil", root, "model")

    def test_trust_remote_code_defaults_off_for_audio(self):
        # AUD-5: audio models don't need remote code -> default OFF.
        os.environ.pop("OMNI_AUDIO_TRUST_REMOTE_CODE", None)
        import importlib
        importlib.reload(self.ace)
        self.assertFalse(self.ace._trust_remote_code())
        os.environ["OMNI_AUDIO_TRUST_REMOTE_CODE"] = "1"
        try:
            self.assertTrue(self.ace._trust_remote_code())
        finally:
            os.environ.pop("OMNI_AUDIO_TRUST_REMOTE_CODE", None)


if __name__ == "__main__":
    unittest.main()
