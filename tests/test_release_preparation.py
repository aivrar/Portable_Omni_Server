"""Exercise image assembly and integrity rejection using tiny offline parts."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class ReleasePreparationTests(unittest.TestCase):
    def prepare(self, root, corrupt=False):
        shutil.copy2(ROOT / "packaging" / "Prepare-Omni.ps1", root)
        parts = root / "linux" / "parts"
        parts.mkdir(parents=True)
        content = (b"first chunk\n", b"second chunk\x00\xff")
        entries = []
        for number, data in enumerate(content, 1):
            name = f"omni-rootfs-v1.0.0.tar.gz.{number:03d}"
            (parts / name).write_bytes(data if not corrupt else b"damaged")
            entries.append({"name": name, "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        image = b"".join(content)
        manifest = {"schema": 1, "tag": "v1.0.0", "image": {
            "size": len(image), "sha256": hashlib.sha256(image).hexdigest(), "parts": entries}}
        (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return image

    def run_script(self, root):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(root / "Prepare-Omni.ps1"), "-Offline"],
            capture_output=True, timeout=30)

    def test_offline_assembly_and_repeat_are_byte_exact(self):
        with tempfile.TemporaryDirectory(prefix="omni release spaces ") as tmp:
            root = Path(tmp)
            image = self.prepare(root)
            for _ in range(2):
                result = self.run_script(root)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual((root / "linux" / "rootfs.tar.gz").read_bytes(), image)

    def test_corrupt_part_does_not_create_importable_image(self):
        with tempfile.TemporaryDirectory(prefix="omni release corrupt ") as tmp:
            root = Path(tmp)
            self.prepare(root, corrupt=True)
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "linux" / "rootfs.tar.gz").exists())

    def test_existing_different_image_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="omni release existing ") as tmp:
            root = Path(tmp)
            self.prepare(root)
            target = root / "linux" / "rootfs.tar.gz"
            target.write_bytes(b"existing studio image")
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), b"existing studio image")


if __name__ == "__main__":
    unittest.main()
