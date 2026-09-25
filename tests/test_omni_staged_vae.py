"""Focused tests for the generic staged VAE decode boundary."""

import sys
import types
import unittest
from unittest import mock

from comfy_nodes.omni_bridge.nodes import omni_staged_vae


class StagedVAEDecodeTests(unittest.TestCase):
    def test_decode_uses_comfy_vaedecode_and_unloads_vae(self):
        events = []
        vae = types.SimpleNamespace(patcher=object())

        def load_vae(name, device):
            events.append(("load-vae", name, device))
            return vae, "/models/vae/flux2-vae.safetensors"

        class FakeVAEDecode:
            def decode(self, vae, samples):
                events.append(("decode", samples))
                return ("images",)

        fake_nodes = types.SimpleNamespace(VAEDecode=FakeVAEDecode)

        with (
            mock.patch.dict(sys.modules, {"nodes": fake_nodes}),
            mock.patch.object(omni_staged_vae, "_load_disk_vae", side_effect=load_vae),
            mock.patch.object(omni_staged_vae, "_release_memory"),
            mock.patch.object(omni_staged_vae, "_unload_patcher") as unload_vae,
            mock.patch.object(omni_staged_vae, "_to_cpu", side_effect=lambda value: value),
        ):
            result = omni_staged_vae.OmniStageVAEDecode().decode(
                {"samples": "latent"},
                "flux2-vae.safetensors",
                "primary",
            )

        self.assertEqual(result, ("images",))
        self.assertEqual(events[0], ("load-vae", "flux2-vae.safetensors", "primary"))
        self.assertEqual(events[1], ("decode", {"samples": "latent"}))
        unload_vae.assert_called_once_with(vae.patcher)


if __name__ == "__main__":
    unittest.main()
