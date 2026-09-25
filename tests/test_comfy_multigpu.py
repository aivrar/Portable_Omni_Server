"""Multi-GPU launch and stable Omni routing contract tests."""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from comfy_manager import normalize_gpu_pool  # noqa: E402


def _load_node_module():
    path = ROOT / "comfy_nodes/omni_bridge/nodes/omni_multigpu.py"
    spec = importlib.util.spec_from_file_location("test_omni_multigpu_nodes", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GPUPoolTests(unittest.TestCase):
    def test_primary_is_first_and_mapping_is_deterministic(self):
        pool, mapping = normalize_gpu_pool(
            "cuda:1", ["cuda:0", "cuda:1", "cuda:2", "cuda:0"],
        )
        self.assertEqual(pool, ["cuda:1", "cuda:0", "cuda:2"])
        self.assertEqual(mapping, {
            "cuda:1": "cuda:0",
            "cuda:0": "cuda:1",
            "cuda:2": "cuda:2",
        })

    def test_legacy_single_gpu_start_remains_isolated(self):
        pool, mapping = normalize_gpu_pool("cuda:3", [])
        self.assertEqual(pool, ["cuda:3"])
        self.assertEqual(mapping, {"cuda:3": "cuda:0"})

    def test_cpu_rejects_gpu_pool(self):
        with self.assertRaisesRegex(ValueError, "only valid"):
            normalize_gpu_pool("cpu", ["cuda:0"])

    def test_pool_rejects_non_cuda_values(self):
        with self.assertRaisesRegex(ValueError, "Invalid gpu_pool"):
            normalize_gpu_pool("cuda:0", ["cuda:1", "cpu"])


class OmniRoutingNodeTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_node_module()

    def test_portable_device_options_follow_runtime_pool(self):
        with mock.patch.dict(os.environ, {
            "OMNI_COMFY_GPU_POOL": '["cuda:2", "cuda:0", "cuda:1"]',
        }):
            self.assertEqual(
                self.module._device_options(),
                ["default", "primary", "auxiliary:1", "auxiliary:2", "cpu"],
            )

    def test_auxiliary_alias_resolves_to_logical_device(self):
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(device_count=lambda: 3),
            device=lambda value: value,
        )
        with mock.patch.dict(sys.modules, {"torch": fake_torch}):
            self.assertEqual(self.module._resolve_device("primary"), "cuda:0")
            self.assertEqual(self.module._resolve_device("auxiliary:2"), "cuda:2")
            with self.assertRaisesRegex(RuntimeError, "exposes 3"):
                self.module._resolve_device("auxiliary:3")

    def test_model_route_requires_safe_reload_capability(self):
        device0 = types.SimpleNamespace(type="cuda", index=0)
        device1 = types.SimpleNamespace(type="cuda", index=1)
        patcher = types.SimpleNamespace(
            model=types.SimpleNamespace(),
            load_device=device0,
            offload_device=types.SimpleNamespace(type="cpu"),
            clone=lambda: None,
        )
        with self.assertRaisesRegex(RuntimeError, "safe multi-GPU reload"):
            self.module._route_patcher(patcher, device1)

    def test_same_device_model_route_preserves_patcher_identity(self):
        device0 = types.SimpleNamespace(type="cuda", index=0)
        patcher = types.SimpleNamespace(
            model=types.SimpleNamespace(),
            load_device=device0,
            offload_device=types.SimpleNamespace(type="cpu"),
            clone=mock.Mock(side_effect=AssertionError("no-op route cloned")),
        )

        routed = self.module._route_patcher(patcher, device0)

        self.assertIs(routed, patcher)
        patcher.clone.assert_not_called()

    def test_clip_route_aligns_encoder_with_routed_patcher_model(self):
        original_model = object()
        routed_model = object()
        cloned_clip = types.SimpleNamespace(
            patcher=object(),
            cond_stage_model=original_model,
        )
        clip = types.SimpleNamespace(clone=lambda: cloned_clip)
        routed_patcher = types.SimpleNamespace(model=routed_model)

        with (
            mock.patch.object(self.module, "_resolve_device", return_value="cuda:1"),
            mock.patch.object(
                self.module,
                "_route_patcher",
                return_value=routed_patcher,
            ),
        ):
            routed, = self.module.OmniRouteCLIP().route(clip, "auxiliary:1")

        self.assertIs(routed.patcher, routed_patcher)
        self.assertIs(routed.cond_stage_model, routed_model)

    def test_audio_encoder_route_aligns_model_and_load_device(self):
        original_model = object()
        routed_model = object()
        encoder = types.SimpleNamespace(
            patcher=types.SimpleNamespace(model=original_model),
            model=original_model,
            load_device="cuda:0",
        )
        routed_patcher = types.SimpleNamespace(
            model=routed_model,
            load_device="cuda:1",
        )

        with (
            mock.patch.object(self.module, "_resolve_device", return_value="cuda:1"),
            mock.patch.object(
                self.module, "_route_audio_encoder_patcher", return_value=routed_patcher,
            ),
        ):
            routed, = self.module.OmniRouteAudioEncoder().route(
                encoder,
                "auxiliary:1",
            )

        self.assertIsNot(routed, encoder)
        self.assertIs(routed.patcher, routed_patcher)
        self.assertIs(routed.model, routed_model)
        self.assertEqual(routed.load_device, "cuda:1")

    def test_audio_encoder_patcher_clone_retargets_before_encode(self):
        device0 = types.SimpleNamespace(type="cuda", index=0)
        device1 = types.SimpleNamespace(type="cuda", index=1)
        offload = types.SimpleNamespace(type="cpu")
        cloned = types.SimpleNamespace(
            model=object(),
            load_device=device0,
            offload_device=offload,
            register_load_device=mock.Mock(),
        )
        patcher = types.SimpleNamespace(
            model=types.SimpleNamespace(),
            load_device=device0,
            offload_device=offload,
            clone=mock.Mock(return_value=cloned),
        )

        routed = self.module._route_audio_encoder_patcher(patcher, device1)

        self.assertIs(routed, cloned)
        self.assertIs(routed.load_device, device1)
        self.assertIs(routed.offload_device, offload)
        routed.register_load_device.assert_called_once_with(device1)

    def test_clip_reload_factory_initializes_on_offload_device(self):
        def factory(first, second, model_options):
            return first, second, model_options

        source = types.SimpleNamespace(cached_patcher_init=None)
        original_options = {"custom_operations": object()}
        patcher = types.SimpleNamespace(
            is_clip=True,
            offload_device="cpu",
            cached_patcher_init=(factory, ("a", "b", original_options)),
            clone=lambda: source,
        )

        routed_source = self.module._retarget_clip_reload_source(
            patcher, "cuda:1",
        )
        routed_options = routed_source.cached_patcher_init[1][2]

        self.assertIs(routed_source, source)
        self.assertNotIn("load_device", original_options)
        self.assertEqual(routed_options["load_device"], "cuda:1")
        self.assertEqual(routed_options["offload_device"], "cpu")
        self.assertEqual(routed_options["initial_device"], "cpu")

    def test_checkpoint_clip_reload_retargets_te_options(self):
        def factory(first, model_options, te_model_options):
            return first, model_options, te_model_options

        source = types.SimpleNamespace(cached_patcher_init=None)
        model_options = {"diffusion": True}
        te_options = {"text": True}
        patcher = types.SimpleNamespace(
            is_clip=True,
            offload_device="cpu",
            cached_patcher_init=(
                factory,
                ("a", model_options, te_options),
            ),
            clone=lambda: source,
        )

        routed_source = self.module._retarget_clip_reload_source(
            patcher, "cuda:1",
        )
        routed_args = routed_source.cached_patcher_init[1]

        self.assertIs(routed_args[1], model_options)
        self.assertNotIn("load_device", te_options)
        self.assertEqual(routed_args[2]["load_device"], "cuda:1")
        self.assertEqual(routed_args[2]["initial_device"], "cpu")


if __name__ == "__main__":
    unittest.main()
