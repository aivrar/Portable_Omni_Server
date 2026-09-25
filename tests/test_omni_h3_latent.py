"""Tests for official MiniMax H3 NestedTensor split/join helpers."""

import unittest

from comfy_nodes.omni_bridge.nodes.omni_h3_latent import (
    OmniH3JoinAVLatent,
    OmniH3SplitAVLatent,
    _nested_parts,
)


class FakeNested:
    def __init__(self, tensors):
        self.tensors = list(tensors)

    def unbind(self):
        return self.tensors


class NestedPartsTests(unittest.TestCase):
    def test_unbind_video_and_audio(self):
        video, audio = _nested_parts(FakeNested(["video", "audio"]))
        self.assertEqual(video, "video")
        self.assertEqual(audio, "audio")

    def test_rejects_plain_tensor(self):
        with self.assertRaisesRegex(ValueError, "NestedTensor"):
            _nested_parts(object())

    def test_rejects_incomplete_pair(self):
        with self.assertRaisesRegex(ValueError, "video and audio"):
            _nested_parts(FakeNested(["video-only"]))


class SplitJoinTests(unittest.TestCase):
    def test_split_preserves_sidecar_keys(self):
        video, audio = OmniH3SplitAVLatent().split({
            "samples": FakeNested(["V", "A"]),
            "batch_index": [0],
        })
        self.assertEqual(video["samples"], "V")
        self.assertEqual(audio["samples"], "A")
        self.assertEqual(video["batch_index"], [0])
        self.assertEqual(audio["batch_index"], [0])

    def test_join_rebuilds_nested_pair(self):
        class FakeNestedTensor:
            def __init__(self, tensors):
                self.tensors = list(tensors)

        fake_module = type("mod", (), {"NestedTensor": FakeNestedTensor})
        import sys
        from unittest import mock

        with mock.patch.dict(sys.modules, {"comfy": fake_module, "comfy.nested_tensor": fake_module}):
            packed, = OmniH3JoinAVLatent().join(
                {"samples": "V", "noise_mask": None},
                {"samples": "A"},
            )
        self.assertEqual(packed["samples"].tensors, ["V", "A"])
        self.assertIsNone(packed["noise_mask"])


if __name__ == "__main__":
    unittest.main()
