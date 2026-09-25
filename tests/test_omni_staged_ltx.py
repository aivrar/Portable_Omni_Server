"""Focused tests for Omni's lossless staged LTX prompt encoder."""

import sys
import types
import unittest
from unittest import mock

from comfy_nodes.omni_bridge.nodes.omni_staged_ltx import (
    OmniLTXStageConditioning,
    OmniLTXStageGuide,
    OmniLTXStageSampler,
)


class StagedLTXConditioningTests(unittest.TestCase):
    def test_preserves_all_options_and_sets_frame_rate(self):
        moved = []

        class FakeTensor:
            def detach(self):
                return self

            def to(self, device):
                moved.append(device)
                return self

        tensor = FakeTensor()

        class FakeClip:
            patcher = object()

            def tokenize(self, text):
                return text

            def encode_from_tokens_scheduled(self, tokens):
                return [[tensor, {"pooled_output": tensor, "custom": "kept"}]]

        fake_sd = types.SimpleNamespace(
            CLIPType=types.SimpleNamespace(LTXV="ltxv"),
            load_clip=lambda **kwargs: FakeClip(),
        )
        fake_paths = types.SimpleNamespace(
            get_full_path_or_raise=lambda *_: "/tmp/ltx.safetensors",
            get_folder_paths=lambda *_: ["/tmp/embeddings"],
        )
        fake_helpers = types.SimpleNamespace(
            conditioning_set_values=lambda cond, values: [
                [item[0], {**item[1], **values}] for item in cond
            ],
        )
        modules = {
            "comfy": types.SimpleNamespace(sd=fake_sd),
            "comfy.sd": fake_sd,
            "folder_paths": fake_paths,
            "node_helpers": fake_helpers,
        }
        with mock.patch.dict(sys.modules, modules), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._model_options_for_device",
            return_value={},
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._unload_patcher",
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._release_memory",
        ):
            positive, negative = OmniLTXStageConditioning().condition(
                "ltx.safetensors", "positive", "negative", 24.0, "primary",
            )

        for conditioning in (positive, negative):
            options = conditioning[0][1]
            self.assertEqual(options["custom"], "kept")
            self.assertEqual(options["frame_rate"], 24.0)
            self.assertIs(options["pooled_output"], tensor)
        self.assertEqual(moved, ["cpu", "cpu", "cpu", "cpu"])


class StagedLTXGuideTests(unittest.TestCase):
    def test_encodes_guide_and_releases_video_vae(self):
        patcher = object()
        vae = types.SimpleNamespace(patcher=patcher)
        calls = []

        class FakeAddGuide:
            @classmethod
            def execute(cls, *args):
                calls.append(args)
                return ("positive-out", "negative-out", {"samples": "latent-out"})

        class FakeSeparate:
            @classmethod
            def execute(cls, latent):
                return ({"samples": "video"}, {"samples": "audio"})

        class FakeConcat:
            @classmethod
            def execute(cls, video, audio):
                return ({"samples": (video["samples"], audio["samples"])},)

        fake_nodes = types.SimpleNamespace(
            LTXVAddGuide=FakeAddGuide,
            LTXVConcatAVLatent=FakeConcat,
            LTXVSeparateAVLatent=FakeSeparate,
        )
        modules = {
            "comfy_extras": types.SimpleNamespace(nodes_lt=fake_nodes),
            "comfy_extras.nodes_lt": fake_nodes,
        }
        with mock.patch.dict(sys.modules, modules), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._load_disk_vae",
            return_value=(vae, "/tmp/video-vae.safetensors"),
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._to_cpu",
            side_effect=lambda value: value,
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._unload_patcher",
        ) as unload, mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._release_memory",
        ) as release:
            result = OmniLTXStageGuide().guide(
                "positive", "negative", {"samples": "latent"}, "image",
                "video-vae.safetensors", 0, 0.8, "auxiliary:1",
            )

        self.assertEqual(result, ("positive-out", "negative-out", {"samples": "latent-out"}))
        self.assertEqual(calls[0][5:7], (0, 0.8))
        unload.assert_called_once_with(patcher)
        release.assert_called_once_with(("/tmp/video-vae.safetensors",))

    def test_splits_and_recombines_nested_av_latent(self):
        nested = types.SimpleNamespace(is_nested=True)
        calls = []

        class FakeAddGuide:
            @classmethod
            def execute(cls, *args):
                calls.append(args)
                return ("positive-out", "negative-out", {"samples": "guided-video"})

        class FakeSeparate:
            @classmethod
            def execute(cls, latent):
                return ({"samples": "video"}, {"samples": "audio"})

        class FakeConcat:
            @classmethod
            def execute(cls, video, audio):
                return ({"samples": (video["samples"], audio["samples"])},)

        fake_nodes = types.SimpleNamespace(
            LTXVAddGuide=FakeAddGuide,
            LTXVConcatAVLatent=FakeConcat,
            LTXVSeparateAVLatent=FakeSeparate,
        )
        modules = {
            "comfy_extras": types.SimpleNamespace(nodes_lt=fake_nodes),
            "comfy_extras.nodes_lt": fake_nodes,
        }
        vae = types.SimpleNamespace(patcher=object())
        with mock.patch.dict(sys.modules, modules), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._load_disk_vae",
            return_value=(vae, "/tmp/video-vae.safetensors"),
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._to_cpu",
            side_effect=lambda value: value,
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._unload_patcher",
        ), mock.patch(
            "comfy_nodes.omni_bridge.nodes.omni_staged_ltx._release_memory",
        ):
            result = OmniLTXStageGuide().guide(
                "positive", "negative", {"samples": nested}, "image",
                "video-vae.safetensors", 0, 1.0, "auxiliary:1",
            )

        self.assertEqual(calls[0][3], {"samples": "video"})
        self.assertEqual(result[2], {"samples": ("guided-video", "audio")})


class StagedLTXSamplerTests(unittest.TestCase):
    def test_crop_guides_preserves_nested_audio_stream(self):
        nested = types.SimpleNamespace(is_nested=True)
        calls = []

        class FakeSeparate:
            @classmethod
            def execute(cls, latent):
                return ({"samples": "sampled-video"}, {"samples": "sampled-audio"})

        class FakeCrop:
            @classmethod
            def execute(cls, positive, negative, video):
                calls.append((positive, negative, video))
                return (positive, negative, {"samples": "cropped-video"})

        class FakeConcat:
            @classmethod
            def execute(cls, video, audio):
                return ({"samples": (video["samples"], audio["samples"])},)

        fake_nodes = types.SimpleNamespace(
            LTXVConcatAVLatent=FakeConcat,
            LTXVCropGuides=FakeCrop,
            LTXVSeparateAVLatent=FakeSeparate,
        )
        modules = {
            "comfy_extras": types.SimpleNamespace(nodes_lt=fake_nodes),
            "comfy_extras.nodes_lt": fake_nodes,
        }
        with mock.patch.dict(sys.modules, modules):
            result = OmniLTXStageSampler._crop_guides(
                {"samples": nested}, "positive", "negative",
            )

        self.assertEqual(calls, [("positive", "negative", {"samples": "sampled-video"})])
        self.assertEqual(result, {"samples": ("cropped-video", "sampled-audio")})


if __name__ == "__main__":
    unittest.main()
