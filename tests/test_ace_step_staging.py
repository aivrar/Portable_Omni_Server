import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import ace_step_loaders


def _weight_dir(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "model.safetensors").write_bytes(b"test")
    return path


class AceStepStagingTests(unittest.TestCase):
    def test_native_stage_contains_every_upstream_main_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / "weights"
            _weight_dir(weights, "acestep-v15-turbo")
            text = _weight_dir(root, "text")
            vae = _weight_dir(root, "vae")
            lm = _weight_dir(root, "lm")
            stage = root / "checkpoints"

            def shared(name):
                return text if name == "Qwen3-Embedding-0.6B" else vae

            with (
                patch.object(ace_step_loaders, "_ACE_V15_STAGE_ROOT", stage),
                patch.object(ace_step_loaders, "_find_shared_ace_component", side_effect=shared),
                patch.object(ace_step_loaders, "ace_step_lm_available_path", return_value=lm),
            ):
                checkpoint_dir, config_name, vae_checkpoint = (
                    ace_step_loaders._prepare_v15_checkpoints(
                        "ace-1.5", {}, weights, "default"
                    )
                )

            self.assertEqual(stage, checkpoint_dir)
            self.assertEqual("acestep-v15-turbo", config_name)
            self.assertEqual(str(vae), vae_checkpoint)
            for component in (
                "acestep-v15-turbo",
                "Qwen3-Embedding-0.6B",
                "vae",
                "acestep-5Hz-lm-1.7B",
            ):
                self.assertTrue((stage / component / "model.safetensors").exists())

    def test_final_native_lora_detach_unwraps_empty_peft_decoder(self):
        base_decoder = object()

        class Decoder:
            def get_base_model(self):
                return base_decoder

        class Model:
            decoder = Decoder()

        class Pipe:
            model = Model()

            def remove_lora(self, name):
                self.removed = name
                return (
                    "Last adapter removed; base decoder still wrapped "
                    "(no backup). Restart or load a new LoRA."
                )

        pipe = Pipe()
        entry = {
            "handle": {
                "kind": "ace_v15",
                "adapter_name": "raspy-vocal-pack",
            }
        }

        ace_step_loaders._detach_lora_handle(entry, pipe=pipe)

        self.assertEqual("raspy-vocal-pack", pipe.removed)
        self.assertIs(base_decoder, pipe.model.decoder)

    def test_failed_final_native_lora_detach_recovers_stale_peft_wrapper(self):
        class BaseDecoder:
            def __init__(self):
                self.loaded = None
                self.eval_called = False

            def load_state_dict(self, state, strict=False):
                self.loaded = (state, strict)

            def to(self, value):
                return self

            def eval(self):
                self.eval_called = True

        base_decoder = BaseDecoder()

        class PeftBase:
            model = base_decoder

        class Decoder:
            base_model = PeftBase()

        class Model:
            decoder = Decoder()

        class Service:
            registry = {"raspy-vocal-pack": object()}
            scale_state = {"raspy-vocal-pack": 0.5}
            active_adapter = "raspy-vocal-pack"
            last_scale_report = {"modified": 1}

        class Pipe:
            model = Model()
            device = "cuda:0"
            dtype = "bf16"
            lora_loaded = True
            use_lora = True
            _adapter_type = "lora"
            _active_loras = {}
            _base_decoder = {"weight": "saved"}
            _lora_service = Service()
            _lora_adapter_registry = {"raspy-vocal-pack": object()}
            _lora_active_adapter = "raspy-vocal-pack"
            _lora_scale_state = {"raspy-vocal-pack": 0.5}

            def remove_lora(self, name):
                return f"Failed to remove LoRA: '{name}'"

        pipe = Pipe()
        entry = {
            "handle": {
                "kind": "ace_v15",
                "adapter_name": "raspy-vocal-pack",
            }
        }

        ace_step_loaders._detach_lora_handle(entry, pipe=pipe)

        self.assertIs(base_decoder, pipe.model.decoder)
        self.assertEqual(({"weight": "saved"}, False), base_decoder.loaded)
        self.assertTrue(base_decoder.eval_called)
        self.assertFalse(pipe.lora_loaded)
        self.assertFalse(pipe.use_lora)
        self.assertEqual({}, pipe._active_loras)
        self.assertIsNone(pipe._base_decoder)
        self.assertIsNone(pipe._lora_service.active_adapter)

    def test_lora_pack_adapter_file_gets_safe_peft_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "adapter_config.json"
            weights = root / "male_vocals_adapter_model.safetensors"
            config.write_text("{}", encoding="utf-8")
            weights.write_bytes(b"weights")
            info = {
                "adapter_files": [
                    {"filename": "male_vocals_adapter_model.safetensors"}
                ]
            }

            view = ace_step_loaders._resolve_lora_adapter_view(
                info, root, "male_vocals_adapter_model.safetensors"
            )

            self.assertEqual(config.read_bytes(), (view / "adapter_config.json").read_bytes())
            self.assertEqual(weights.read_bytes(), (view / "adapter_model.safetensors").read_bytes())
            with self.assertRaises(ValueError):
                ace_step_loaders._resolve_lora_adapter_view(
                    info, root, "../male_vocals_adapter_model.safetensors"
                )
            with self.assertRaises(ValueError):
                ace_step_loaders._resolve_lora_adapter_view(
                    info, root, "undeclared.safetensors"
                )


if __name__ == "__main__":
    unittest.main()
