"""Structured ComfyUI startup flag regression tests."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from comfy_startup import build_startup_args, startup_catalog  # noqa: E402


class ComfyStartupOptionTests(unittest.TestCase):
    def test_catalog_does_not_expose_argv_implementation(self):
        groups = startup_catalog()
        self.assertGreaterEqual(len(groups), 4)
        for group in groups:
            for option in group["options"]:
                self.assertNotIn("flag", option)
                for choice in option.get("choices", []):
                    self.assertNotIn("args", choice)

    def test_builds_allowlisted_flags(self):
        argv, normalized = build_startup_args({
            "cache_policy": "lru_50",
            "attention": "pytorch",
            "reserve_vram": 2.5,
            "disable_all_custom_nodes": True,
            "preview_size": 768,
        })
        self.assertEqual(normalized["reserve_vram"], 2.5)
        self.assertIn("--cache-lru", argv)
        self.assertIn("--use-pytorch-cross-attention", argv)
        self.assertIn("--reserve-vram", argv)
        self.assertIn("--disable-all-custom-nodes", argv)
        self.assertIn("--preview-size", argv)

    def test_sage_attention_maps_to_exact_comfy_flag(self):
        argv, normalized = build_startup_args({"attention": "sage"})
        self.assertEqual(argv, ["--use-sage-attention"])
        self.assertEqual(normalized["attention"], "sage")

    def test_rejects_unknown_option(self):
        with self.assertRaisesRegex(ValueError, "Unknown ComfyUI startup option"):
            build_startup_args({"listen": "0.0.0.0"})

    def test_rejects_invalid_select_value(self):
        with self.assertRaisesRegex(ValueError, "Invalid attention"):
            build_startup_args({"attention": "made-up-backend"})

    def test_rejects_out_of_range_number(self):
        with self.assertRaisesRegex(ValueError, "reserve_vram must be between"):
            build_startup_args({"reserve_vram": 999})

    def test_boolean_is_strict(self):
        with self.assertRaisesRegex(ValueError, "must be true or false"):
            build_startup_args({"deterministic": "yes"})

    def test_default_values_do_not_add_unnecessary_flags(self):
        argv, _ = build_startup_args({
            "cache_policy": "auto",
            "preview_size": 512,
            "max_upload_size": 100,
            "verbose": "INFO",
        })
        self.assertEqual(argv, [])


if __name__ == "__main__":
    unittest.main()
