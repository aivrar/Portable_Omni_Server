import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import ace_step  # noqa: E402
from fastapi import HTTPException


class AceStepAudioSourceTests(unittest.TestCase):
    def test_general_omni_output_url_can_feed_ace(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            source = output / "omni" / "tts" / "moss_tts" / "voice.wav"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"RIFF-test-audio")

            with patch.object(ace_step, "OUTPUT_DIR", output):
                encoded = ace_step._resolve_init_audio(
                    None,
                    "/api/outputs/tts/moss_tts/voice.wav?kind=omni",
                )

            self.assertEqual(b"RIFF-test-audio", base64.b64decode(encoded))

    def test_general_output_url_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ace_step, "OUTPUT_DIR", Path(tmp)):
                with self.assertRaises(HTTPException) as error:
                    ace_step._resolve_init_audio(
                        None,
                        "/api/outputs/../secret.wav?kind=omni",
                    )
                self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
