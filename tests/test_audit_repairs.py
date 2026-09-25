"""Model-free regressions for the September audit repairs.

Every filesystem mutation uses a temporary directory. No gateway lifespan,
worker process, model load, or shutdown path is invoked.
"""

import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from fastapi import HTTPException
from helpers import safe_child_path, safe_subtree_path
from key_store import ApiKeyStore
from output_meta import OutputMetaStore
from proxy import _filter_request_headers
from security import required_scope, origin_matches_host
from cli.client import resolve_token, cache_user_token
from routers import outputs


class AuditSecurityTests(unittest.TestCase):
    def test_root_is_never_a_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in (".", "..", "", "C:", "file:stream"):
                with self.subTest(name=name), self.assertRaises(HTTPException):
                    safe_child_path(Path(tmp), name)

    def test_in_tree_link_keeps_entry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "target"
            target.write_text("keep", encoding="utf-8")
            link = root / "alias"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            for helper in (safe_child_path, safe_subtree_path):
                self.assertEqual(helper(root, "alias"), link)
                self.assertTrue(helper(root, "alias").is_symlink())
            safe_child_path(root, "alias").unlink()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_proxy_removes_credentials(self):
        clean = _filter_request_headers({"Authorization": "test", "X-Omni-Token": "test",
                                         "Cookie": "test", "Range": "bytes=0-1"})
        self.assertEqual(clean, {"Range": "bytes=0-1"})

    def test_scope_boundaries(self):
        cases = [("GET", "/api/workers", "read"),
                 ("POST", "/api/workers/spawn", "manage"),
                 ("POST", "/api/chat/qwen/stream", "generate"),
                 ("POST", "/api/workflows/w.json/run", "generate"),
                 ("POST", "/api/workflows/analyze", "read"),
                 ("DELETE", "/api/keys/id", "admin")]
        for method, path, expected in cases:
            self.assertEqual(required_scope(method, path), expected)

    def test_remote_origin_requires_opt_in_and_host_match(self):
        self.assertFalse(origin_matches_host("https://host:9200", "host:9200", allow_remote=False))
        self.assertTrue(origin_matches_host("https://host:9200", "host:9200", allow_remote=True))
        self.assertFalse(origin_matches_host("https://evil:9200", "host:9200", allow_remote=True))

    def test_remote_cli_never_uses_local_token_files(self):
        with patch.dict(os.environ, {}, clear=True), patch("cli.client._read_secret") as read:
            with self.assertRaises(RuntimeError):
                resolve_token(None, "https://remote.example")
            read.assert_not_called()

    def test_cli_cache_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as tmp, patch("cli.client._user_token_path", return_value=Path(tmp) / "token"):
            cache_user_token("test-only-credential")
            p = Path(tmp) / "token"
            self.assertEqual(p.read_text().strip(), "test-only-credential")
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class AuditPersistenceTests(unittest.TestCase):
    def test_bad_store_shapes_do_not_crash_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for payload in ([], 1, None, {"keys": 3, "records": 3}, {"keys": [1], "records": [1]}):
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(ApiKeyStore(path).list(), [])
                self.assertEqual(OutputMetaStore(path).list(), [])

    def test_failed_revocation_is_reported_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ApiKeyStore(Path(tmp) / "keys.json")
            key, secret = store.create(["read"])
            with patch("key_store.os.replace", side_effect=OSError("test disk failure")):
                with self.assertRaises(OSError):
                    store.revoke(key.id)
            self.assertIsNotNone(store.authenticate(secret))
            self.assertIsNotNone(ApiKeyStore(store.path).authenticate(secret))
            self.assertTrue(store.revoke(key.id))
            self.assertIsNone(ApiKeyStore(store.path).authenticate(secret))
            with self.assertRaises(ValueError):
                store.create([])

    def test_failed_pin_update_preserves_memory_and_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OutputMetaStore(Path(tmp) / "meta.json")
            store.upsert("a.png", pinned=True)
            with patch("output_meta.os.replace", side_effect=OSError("test disk failure")):
                with self.assertRaises(OSError):
                    store.upsert("a.png", pinned=False)
            self.assertTrue(store.get("a.png").pinned)
            self.assertTrue(OutputMetaStore(store.path).get("a.png").pinned)

    def test_metadata_roots_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            path.write_text(json.dumps({"records": [{"path": "same.png", "pinned": True}]}))
            store = OutputMetaStore(path)
            self.assertTrue(store.get("same.png", kind="output").pinned)
            self.assertIsNone(store.get("same.png", kind="input"))
            store.upsert("same.png", kind="input", tags=["input-tag"])
            store = OutputMetaStore(path)
            self.assertEqual(store.get("same.png", kind="input").tags, ["input-tag"])
            store.delete("same.png", kind="input")
            self.assertTrue(store.get("same.png").pinned)


class AuditOutputTests(unittest.TestCase):
    def test_range_clamps_end(self):
        self.assertEqual(outputs._parse_range("bytes=2-9999", 10), (2, 9))
        with self.assertRaises(HTTPException):
            outputs._parse_range("bytes=10-9999", 10)

    def test_compressed_png_metadata_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.png"
            data = b"prompt\0\0" + zlib.compress(b"a" * 100000)
            path.write_bytes(outputs._PNG_SIGNATURE + struct.pack(">I", len(data)) + b"zTXt" + data + b"\0" * 4)
            self.assertEqual(outputs._read_png_text_chunks(path, max_bytes=1024), {})

    def test_sort_and_pagination_cover_older_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(outputs._OUTPUT_ROOTS, {"output": Path(tmp)}):
            root = Path(tmp)
            for name in ("c.txt", "a.txt", "b.txt"):
                (root / name).write_text(name)
            kw = dict(kind="output", subdir="", since=None, limit=2, prefix="", media_kind=None,
                      prompt_id=None, tag=None, pinned=None, collection=None, sort="name")
            first = asyncio.run(outputs.list_outputs(**kw))
            self.assertEqual([r["path"] for r in first["files"]], ["a.txt", "b.txt"])
            second = asyncio.run(outputs.list_outputs(**kw, offset=first["next_offset"]))
            self.assertEqual([r["path"] for r in second["files"]], ["c.txt"])
            self.assertIsNone(second["next_offset"])


if __name__ == "__main__":
    unittest.main()
