import struct
import json
import sys
import tempfile
import unittest
import wave
import zlib
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers.outputs import _file_record, _readable_document, _related_media


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _minimal_png(width: int, height: int, color_type: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IEND", b"")


class OutputArtifactMetadataTests(unittest.TestCase):
    def test_png_probe_reports_dimensions_and_alpha(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rgba.png"
            target.write_bytes(_minimal_png(640, 480, 6))
            artifact = _file_record(root, target, probe=True)["artifact"]
            self.assertEqual(artifact["dimensions"], {"width": 640, "height": 480})
            self.assertEqual(artifact["metadata"]["channels"], 4)
            self.assertTrue(artifact["metadata"]["has_alpha"])
            self.assertEqual(artifact["integrity"]["status"], "ok")

    def test_truncated_png_is_explicitly_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "truncated.png"
            target.write_bytes(_minimal_png(64, 64, 2)[:-12])

            artifact = _file_record(root, target, probe=True)["artifact"]

            self.assertEqual(artifact["integrity"]["status"], "invalid")
            self.assertIn("IEND", artifact["integrity"]["detail"])

    def test_png_probe_reports_embedded_comfy_provenance_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "comfy.png"
            base = _minimal_png(64, 64, 2)
            prompt = _png_chunk(b"tEXt", b"prompt\x00{\"1\": {}}")
            target.write_bytes(base[:-12] + prompt + base[-12:])

            artifact = _file_record(root, target, probe=True)["artifact"]

            self.assertEqual(artifact["provenance"]["source"], "comfy-png")
            self.assertEqual(artifact["provenance"]["embedded_keys"], ["prompt"])

    def test_wav_probe_reports_duration_and_audio_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.wav"
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b"\x00\x00" * 2 * 4000)
            artifact = _file_record(root, target, probe=True)["artifact"]
            self.assertAlmostEqual(artifact["duration"], 0.5)
            self.assertEqual(artifact["metadata"]["channels"], 2)
            self.assertEqual(artifact["metadata"]["sample_rate"], 8000)
            self.assertEqual(artifact["integrity"]["status"], "ok")

    def test_same_directory_manifest_adds_bounded_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "job-123"
            job.mkdir()
            target = job / "candidate.wav"
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b"\x00\x00" * 80)
            (job / "manifest.json").write_text(json.dumps({
                "job_id": "job-123",
                "mode": "generate-ranked",
                "model_variant": "sao-open-small",
                "results": [{
                    "filename": "candidate.wav", "seed": 42, "score": 0.75,
                }],
            }), encoding="utf-8")

            provenance = _file_record(root, target, probe=True)["artifact"]["provenance"]

            self.assertEqual(provenance["job_id"], "job-123")
            self.assertEqual(provenance["manifest"], "job-123/manifest.json")
            self.assertEqual(provenance["result"]["seed"], 42)

    def test_json_document_is_readable_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manifest.json"
            target.write_text(json.dumps({"model": "ltx-2.5", "steps": 30}), encoding="utf-8")

            document = _readable_document(target)

            self.assertEqual(document["parsed"]["model"], "ltx-2.5")
            self.assertFalse(document["truncated"])
            self.assertIn('"steps": 30', document["text"])

    def test_related_media_lists_only_playable_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            (root / "preview.mp4").write_bytes(b"video")
            (root / "notes.txt").write_text("details", encoding="utf-8")

            related = _related_media(root, manifest)

            self.assertEqual([row["path"] for row in related], ["preview.mp4"])
            self.assertEqual(related[0]["kind"], "video")


if __name__ == "__main__":
    unittest.main()
