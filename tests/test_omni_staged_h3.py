"""Focused tests for Omni's staged MiniMax H3 helpers."""

import sys
import types
import unittest
from unittest import mock

from comfy_nodes.omni_bridge.nodes.omni_staged_h3 import (
    _apply_h3_lora,
    _apply_h3_low_vram_attention,
    _apply_h3_sage_attention,
    _apply_h3_sigma_shift,
    _apply_spectrum_h3,
)


class SpectrumIntegrationTests(unittest.TestCase):
    def test_disabled_spectrum_returns_original_model(self):
        model = object()

        result = _apply_spectrum_h3(
            model,
            enabled=False,
            blend_weight=0.5,
            audio_blend_weight=0.0,
            offline_smoothing_replay=True,
        )

        self.assertIs(result, model)

    def test_enabled_spectrum_uses_ram_safe_settings(self):
        calls = []

        class FakeSpectrum:
            def apply(self, *args, **kwargs):
                calls.append((args, kwargs))
                return ("patched-model",)

        fake_nodes = types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={"SpectrumApplyMiniMaxH3": FakeSpectrum},
        )
        with mock.patch.dict(sys.modules, {"nodes": fake_nodes}):
            result = _apply_spectrum_h3(
                "base-model",
                enabled=True,
                blend_weight=0.45,
                audio_blend_weight=0.0,
                offline_smoothing_replay=True,
            )

        self.assertEqual(result, "patched-model")
        args, kwargs = calls[0]
        self.assertEqual(args[0], "base-model")
        self.assertEqual(args[2], 0.45)
        self.assertEqual(kwargs["history_storage"], "system_ram")
        self.assertEqual(kwargs["offline_archive_storage"], "system_ram")
        self.assertTrue(kwargs["offline_smoothing_replay"])
        self.assertEqual(kwargs["audio_blend_weight"], 0.0)

    def test_missing_spectrum_node_fails_clearly(self):
        fake_nodes = types.SimpleNamespace(NODE_CLASS_MAPPINGS={})
        with mock.patch.dict(sys.modules, {"nodes": fake_nodes}):
            with self.assertRaisesRegex(RuntimeError, "SpectrumApplyMiniMaxH3"):
                _apply_spectrum_h3(
                    "base-model",
                    enabled=True,
                    blend_weight=0.5,
                    audio_blend_weight=0.0,
                    offline_smoothing_replay=True,
                )


class TurboIntegrationTests(unittest.TestCase):
    def test_disabled_lora_keeps_original_model(self):
        model = object()

        result, loader, path = _apply_h3_lora(model, "none", 1.0)

        self.assertIs(result, model)
        self.assertIsNone(loader)
        self.assertEqual(path, "")

    def test_lora_uses_model_only_loader(self):
        calls = []

        class FakeLoader:
            def load_lora_model_only(self, model, name, strength):
                calls.append((model, name, strength))
                return ("patched-model",)

        fake_nodes = types.SimpleNamespace(
            LoraLoaderModelOnly=FakeLoader,
            NODE_CLASS_MAPPINGS={},
        )
        fake_paths = types.SimpleNamespace(
            get_full_path_or_raise=lambda category, name: f"/{category}/{name}",
        )
        with mock.patch.dict(sys.modules, {
            "nodes": fake_nodes,
            "folder_paths": fake_paths,
        }):
            result, loader, path = _apply_h3_lora(
                "base-model", "turbo.safetensors", 0.75,
            )

        self.assertEqual(result, "patched-model")
        self.assertIsInstance(loader, FakeLoader)
        self.assertEqual(path, "/loras/turbo.safetensors")
        self.assertEqual(calls, [("base-model", "turbo.safetensors", 0.75)])

    def test_sigma_shift_delegates_to_comfy_core(self):
        calls = []

        class FakeSigmaShift:
            @classmethod
            def execute(cls, model, shift_video, shift_audio):
                calls.append((model, shift_video, shift_audio))
                return ("shifted-model",)

        fake_module = types.SimpleNamespace(MiniMaxH3SigmaShift=FakeSigmaShift)
        with mock.patch.dict(sys.modules, {
            "comfy_extras": types.SimpleNamespace(nodes_minimax_h3=fake_module),
            "comfy_extras.nodes_minimax_h3": fake_module,
        }):
            result = _apply_h3_sigma_shift("base-model", 12.0, 6.0)

        self.assertEqual(result, "shifted-model")
        self.assertEqual(calls, [("base-model", 12.0, 6.0)])

    def test_disabled_sage_keeps_original_model(self):
        model = object()

        result = _apply_h3_sage_attention(model, "disabled", False)

        self.assertIs(result, model)

    def test_disabled_low_vram_attention_keeps_original_model(self):
        model = object()

        result = _apply_h3_low_vram_attention(model, False, 4)

        self.assertIs(result, model)

    def test_low_vram_attention_uses_kjnodes_patch(self):
        calls = []

        class FakeLowVRAM:
            @classmethod
            def execute(cls, model, head_chunks):
                calls.append((model, head_chunks))
                return ("low-vram-model",)

        fake_nodes = types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={"MiniMaxLowVRAMAttention": FakeLowVRAM},
        )
        with mock.patch.dict(sys.modules, {"nodes": fake_nodes}):
            result = _apply_h3_low_vram_attention("base-model", True, 8)

        self.assertEqual(result, "low-vram-model")
        self.assertEqual(calls, [("base-model", 8)])

    def test_missing_low_vram_attention_fails_clearly(self):
        fake_nodes = types.SimpleNamespace(NODE_CLASS_MAPPINGS={})
        with mock.patch.dict(sys.modules, {"nodes": fake_nodes}):
            with self.assertRaisesRegex(RuntimeError, "MiniMaxLowVRAMAttention"):
                _apply_h3_low_vram_attention("base-model", True, 4)

    def test_sage_uses_kjnodes_model_patch(self):
        calls = []

        class FakeSage:
            def patch(self, model, mode, allow_compile):
                calls.append((model, mode, allow_compile))
                return ("sage-model",)

        fake_nodes = types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={"PathchSageAttentionKJ": FakeSage},
        )
        with mock.patch.dict(sys.modules, {"nodes": fake_nodes}):
            result = _apply_h3_sage_attention("base-model", "auto", False)

        self.assertEqual(result, "sage-model")
        self.assertEqual(calls, [("base-model", "auto", False)])


if __name__ == "__main__":
    unittest.main()
