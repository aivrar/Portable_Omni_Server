import io
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from moss_tts_loaders import _load_runtime_with_fixed_tokenizer
from fastapi import HTTPException
from routers.audio import TTSRequest, _atempo_filter, _do_tts, _retime_moss_wav


class _OriginalProcessor:
    seen_kwargs = None

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        cls.seen_kwargs = kwargs
        return object()


class _FakeStreaming:
    AutoProcessor = _OriginalProcessor

    @classmethod
    def load_runtime(cls, **kwargs):
        cls.AutoProcessor.from_pretrained("fixture", trust_remote_code=True)
        return kwargs


class MossTtsContractTests(unittest.TestCase):
    def test_native_tts_request_accepts_explicit_device(self):
        req = TTSRequest(text="hello", device="cuda:1")
        self.assertEqual("cuda:1", req.device)

    def test_runtime_strips_codec_incompatible_regex_kwarg_and_restores_processor(self):
        result = _load_runtime_with_fixed_tokenizer(_FakeStreaming, device="cuda:0")
        self.assertEqual(result, {"device": "cuda:0"})
        self.assertIs(_FakeStreaming.AutoProcessor, _OriginalProcessor)
        self.assertNotIn("fix_mistral_regex", _OriginalProcessor.seen_kwargs)

    def test_atempo_chain_covers_public_speed_range(self):
        self.assertEqual(_atempo_filter(0.25), "atempo=0.5,atempo=0.5")
        self.assertEqual(_atempo_filter(1.0), "atempo=1")
        self.assertEqual(_atempo_filter(4.0), "atempo=2,atempo=2")

    def test_retime_changes_duration_without_changing_wav_rate(self):
        frames = b"\x00\x00" * 8000
        source = io.BytesIO()
        with wave.open(source, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(frames)

        try:
            output = _retime_moss_wav(source.getvalue(), 2.0)
        except RuntimeError as exc:
            if "FFmpeg is required" in str(exc):
                self.skipTest(str(exc))
            raise

        with wave.open(io.BytesIO(output), "rb") as wav:
            self.assertEqual(wav.getframerate(), 8000)
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 0.5, delta=0.05)


class MossTtsFailureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_side_500_retires_poisoned_worker(self):
        class FakeResponse:
            status_code = 500
            text = "CUDA driver error: unknown error"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        worker = types.SimpleNamespace(worker_id="moss_tts-1", port=8201)
        with mock.patch(
            "routers.audio._resolve_busy_worker",
            new=mock.AsyncMock(return_value=(worker, "job-1")),
        ), mock.patch(
            "routers.audio.httpx.AsyncClient",
            return_value=FakeClient(),
        ), mock.patch(
            "routers.audio._retire_timed_out_worker",
            new=mock.AsyncMock(),
        ) as retire, mock.patch("routers.audio._release_worker") as release:
            with self.assertRaises(HTTPException) as raised:
                await _do_tts(
                    model="moss_tts", text="hello", voice=None,
                    response_format="wav", speed=1.0,
                    autospawn=False, device="cuda:1", model_params=None,
                )

        self.assertEqual(raised.exception.status_code, 500)
        retire.assert_awaited_once_with("moss_tts-1")
        release.assert_called_once_with("moss_tts-1")


if __name__ == "__main__":
    unittest.main()
