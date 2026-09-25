import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from omni_outputs import OMNI_ROOT, omni_output_headers
from routers.moss import MossSfxRequest


class MossSfxContractTests(unittest.TestCase):
    def test_request_accepts_explicit_device_and_short_effect(self):
        req = MossSfxRequest(
            prompt="a single dry wooden door slam",
            seconds=1.5,
            steps=12,
            device="cuda:1",
        )
        self.assertEqual("cuda:1", req.device)
        self.assertEqual(1.5, req.seconds)

    def test_request_rejects_out_of_contract_duration(self):
        with self.assertRaises(ValidationError):
            MossSfxRequest(prompt="rain", seconds=30.1)

    def test_persisted_output_headers_are_media_library_addresses(self):
        target = OMNI_ROOT / "sfx" / "moss_sfx" / "fixture.wav"
        self.assertEqual(
            {
                "X-Omni-Output-Path": "sfx/moss_sfx/fixture.wav",
                "X-Omni-Output-Ref": "omni://outputs/sfx/moss_sfx/fixture.wav",
                "X-Omni-Output-URL": (
                    "/api/outputs/sfx/moss_sfx/fixture.wav?kind=omni"
                ),
            },
            omni_output_headers(target),
        )

    def test_output_headers_reject_paths_outside_omni_library(self):
        self.assertEqual({}, omni_output_headers(OMNI_ROOT.parent / "outside.wav"))


if __name__ == "__main__":
    unittest.main()
