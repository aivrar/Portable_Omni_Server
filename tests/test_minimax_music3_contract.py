import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import config
import minimax_music3_loaders as loaders
from routers.minimax_music3 import _GenerateRequest, _LoadRequest
from routers.setup import _classify_minimax_music3_candidate


class MiniMaxMusic3ContractTests(unittest.TestCase):
    def test_typed_generation_contract_enforces_source_duration_ceiling(self):
        request = _GenerateRequest(prompt="minimal techno", lyrics="[intro]\n(instrumental)")
        self.assertEqual(request.duration_s, 60.0)
        with self.assertRaises(Exception):
            _GenerateRequest(
                prompt="minimal techno",
                lyrics="[intro]\n(instrumental)",
                duration_s=361,
            )

    def test_load_defaults_to_safe_24gb_mode(self):
        request = _LoadRequest()
        self.assertEqual(request.model_variant, "official-diffusers")
        self.assertTrue(request.bf16)
        self.assertTrue(request.cpu_offload)
        self.assertEqual(request.cpu_memory_mb, 16000)
        self.assertEqual(request.reserve_mb, 1024)

    def test_install_check_requires_marker_and_critical_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "models" / "official-diffusers"
            required = (
                ".install_complete",
                "modular_model_index.json",
                "language_model/model.safetensors.index.json",
                "transformer/diffusion_pytorch_model.safetensors.index.json",
                "rvq_depth_decoder/diffusion_pytorch_model.safetensors",
                "vocoder/diffusion_pytorch_model.safetensors",
            )
            for name in required:
                path = model / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            with mock.patch.object(config, "MINIMAX_MUSIC3_ROOT", root):
                self.assertTrue(config.is_minimax_music3_model_installed("official-diffusers"))
                (model / "vocoder/diffusion_pytorch_model.safetensors").unlink()
                self.assertFalse(config.is_minimax_music3_model_installed("official-diffusers"))

    def test_discovery_never_claims_unverified_lora_compatibility(self):
        official = _classify_minimax_music3_candidate(
            "MiniMaxAI/MiniMax-Music3", ["diffusers"], "model"
        )
        lora = _classify_minimax_music3_candidate(
            "someone/music3-style", ["peft"], "lora"
        )
        aoti = _classify_minimax_music3_candidate(
            "diffusers/MiniMax-Music3-aoti", [], "model"
        )
        self.assertTrue(official["runtime_compatible"])
        self.assertFalse(lora["runtime_compatible"])
        self.assertEqual(aoti["compatibility"], "hardware-specific-supplement")

    def test_generation_writes_direct_file_without_base64(self):
        class FakeAudio:
            ndim = 2
            shape = (2, 44100)

            @property
            def T(self):
                return self

            def __len__(self):
                return 44100

        class FakePipe:
            def __call__(self, **kwargs):
                self.kwargs = kwargs
                return [FakeAudio()]

        writes = []
        fake_soundfile = types.SimpleNamespace(
            write=lambda path, audio, rate, subtype: writes.append((path, rate, subtype))
        )
        fake_torch = types.SimpleNamespace(
            Generator=lambda: types.SimpleNamespace(manual_seed=lambda seed: seed)
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            loaders, "OUTPUT_DIR", Path(tmp)
        ), mock.patch.dict(
            sys.modules, {"soundfile": fake_soundfile, "torch": fake_torch}
        ):
            state = loaders.init_state("cuda:0")
            state["pipeline"] = FakePipe()
            result = loaders.infer_generate(state, {
                "job_id": "abcdef123456",
                "prompt": "minimal techno",
                "lyrics": "[intro]\n(instrumental)",
                "duration_s": 1,
                "seed": 9,
            })
        self.assertEqual(result["filename"], "01.wav")
        self.assertEqual(writes[0][1:], (44100, "PCM_24"))
        self.assertNotIn("audio_base64", result)

    def test_load_uses_modular_components_manager_for_cpu_offload(self):
        events = []

        class FakeCuda:
            @staticmethod
            def is_available(): return True
            @staticmethod
            def empty_cache(): pass
            @staticmethod
            def ipc_collect(): pass

        class FakeManager:
            def enable_auto_cpu_offload(self, device):
                events.append(("offload", device))

        class FakePipe:
            sampling_rate = 44100

            @classmethod
            def from_pretrained(cls, path, components_manager=None):
                events.append(("from_pretrained", path, components_manager is not None))
                return cls()

            def load_components(self, dtype):
                events.append(("load_components", dtype))

            def update_components(self, **components):
                events.append(("update_components", sorted(components)))

            def to(self, device):
                events.append(("to", device))

        fake_torch = types.SimpleNamespace(
            cuda=FakeCuda(), bfloat16="bf16", float32="float32"
        )
        fake_diffusers = types.SimpleNamespace(
            ComponentsManager=FakeManager, ModularPipeline=FakePipe
        )
        fake_transformers = types.SimpleNamespace(
            AutoTokenizer=types.SimpleNamespace(
                from_pretrained=lambda *args, **kwargs: "fast-tokenizer"
            )
        )
        state = loaders.init_state("cuda:0")
        with mock.patch.object(
            loaders, "is_minimax_music3_model_installed", return_value=True
        ), mock.patch.object(
            loaders, "minimax_music3_model_path", return_value=Path("/fixture")
        ), mock.patch.dict(
            sys.modules, {
                "torch": fake_torch,
                "diffusers": fake_diffusers,
                "transformers": fake_transformers,
            }
        ):
            result = loaders.load_model(
                state, "official-diffusers", bf16=True, cpu_offload=True
            )
        self.assertTrue(result["model_loaded"])
        self.assertIn(("offload", "cuda:0"), events)
        self.assertIn(("update_components", ["tokenizer"]), events)
        self.assertNotIn(("to", "cuda:0"), events)


if __name__ == "__main__":
    unittest.main()
