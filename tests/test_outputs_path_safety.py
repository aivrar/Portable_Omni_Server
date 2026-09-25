"""Path-safety regression tests for the outputs router.

The outputs API can list, stream, delete, and zip files under whitelisted
roots. Each user-supplied path passes through ``helpers.safe_subtree_path``;
this test pins the exact set of strings we must reject.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi import HTTPException  # noqa: E402

from helpers import safe_child_path, safe_subtree_path  # noqa: E402


class SafeSubtreePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        (self.base / "ok").mkdir()
        (self.base / "ok" / "file.bin").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple_subtree_resolves(self):
        out = safe_subtree_path(self.base, "ok/file.bin")
        self.assertEqual(out, (self.base / "ok" / "file.bin").resolve())

    def test_rejects_dotdot_segment(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "../escape.bin")
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "ok/../etc/passwd")

    def test_rejects_absolute_path(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "/etc/passwd")

    def test_rejects_drive_letter(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "C:/Windows/system32")

    def test_rejects_backslash(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "ok\\file.bin")

    def test_rejects_nul_and_empty(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "")
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "ok/\x00file")

    def test_rejects_empty_segment(self):
        with self.assertRaises(HTTPException):
            safe_subtree_path(self.base, "ok//file.bin")

    @unittest.skipIf(os.name == "nt", "POSIX-only symlink test")
    def test_rejects_escaping_symlink(self):
        outside = self.base.parent / "outside-secret"
        outside.write_bytes(b"secret")
        try:
            link = self.base / "evil.lnk"
            os.symlink(str(outside), str(link))
            with self.assertRaises(HTTPException):
                # safe_subtree_path resolves and checks the resolved path
                safe_subtree_path(self.base, "evil.lnk")
        finally:
            if outside.exists():
                outside.unlink()


class SafeChildPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple_child(self):
        out = safe_child_path(self.base, "file.json", suffix=".json")
        self.assertEqual(out.parent, self.base)

    def test_rejects_traversal(self):
        for bad in ("../escape", "..", "a/b", "a\\b", "\x00", ""):
            with self.assertRaises(HTTPException):
                safe_child_path(self.base, bad)

    def test_rejects_wrong_suffix(self):
        with self.assertRaises(HTTPException):
            safe_child_path(self.base, "file.txt", suffix=".json")


if __name__ == "__main__":
    unittest.main()
