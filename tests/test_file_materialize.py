"""Cache-safe model materialization regression tests."""

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from file_materialize import materialize_cached_file  # noqa: E402


class FileMaterializeTests(unittest.TestCase):
    def test_same_filesystem_uses_independently_deletable_hardlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "cache/blob"
            destination = root / "models/model.safetensors"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"immutable model")

            mode = materialize_cached_file(source, destination)

            self.assertEqual(mode, "hardlink")
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)
            source.unlink()
            self.assertEqual(destination.read_bytes(), b"immutable model")

    def test_cross_filesystem_fallback_streams_atomic_real_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "cache/blob"
            destination = root / "models/model.safetensors"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fallback model")
            real_link = __import__("os").link

            def link_with_exdev(src, dst):
                if Path(dst).name.endswith(".part"):
                    raise OSError(errno.EXDEV, "cross-device link")
                return real_link(src, dst)

            with mock.patch("file_materialize.os.link", side_effect=link_with_exdev):
                mode = materialize_cached_file(source, destination)

            self.assertEqual(mode, "copy")
            self.assertEqual(destination.read_bytes(), b"fallback model")
            self.assertNotEqual(source.stat().st_ino, destination.stat().st_ino)
            self.assertFalse(destination.with_name(destination.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
