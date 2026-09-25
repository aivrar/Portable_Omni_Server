"""Admission regressions: synthetic graphs only; no weights or workers loaded."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_comfy_placement import DEVICES, _instance
from comfy_placement import build_placement_plan, discover_components, _workflow_weight_footprint_mb
from omni_placement import analyze_worker_placement, estimate_model_vram_mb


class PlacementRepairs(unittest.TestCase):
    def plan(self, graph, policy=None, devices=None):
        with tempfile.TemporaryDirectory() as tmp:
            return build_placement_plan(graph, policy or {"mode": "auto", "reserve_mb": 0}, _instance(), devices or DEVICES, model_root=Path(tmp))

    def test_shared_binding_uses_combined_capacity(self):
        graph = {"1": {"class_type": "OmniLTXStageDurationPredictor", "inputs": {}}}
        with patch("comfy_placement._estimate_component", side_effect=lambda c, _: 11000 if c["role"] == "model" else 2000):
            plan = self.plan(graph)
        self.assertTrue(plan["valid"], plan["blockers"])
        self.assertEqual({c["device"]["stable_id"] for c in plan["components"]}, {"GPU-3090"})
        conflict = self.plan(graph, {"overrides": {"1:model": "GPU-3090", "1:duration_head": "GPU-3060"}})
        self.assertFalse(conflict["valid"])

    def test_zero_vram_is_not_total_capacity(self):
        devices = copy.deepcopy(DEVICES)
        for item in devices:
            item["vram_free_mb"] = 0
        plan = self.plan({"1": {"class_type": "UNETLoader", "inputs": {}}}, devices=devices)
        self.assertFalse(plan["valid"])
        self.assertTrue(all(d["usable_mb"] == 0 for d in plan["devices"]))

    def test_cpu_stage_is_bounded(self):
        with patch("comfy_placement._host_model_budget_mb", return_value=1024):
            plan = self.plan({"1": {"class_type": "OmniStageVAEDecode", "inputs": {}}}, {"allow_cpu": True, "overrides": {"1": "cpu"}})
        self.assertFalse(plan["valid"])

    def test_unknown_override_is_blocker(self):
        self.assertFalse(self.plan({"1": {"class_type": "UNETLoader", "inputs": {}}}, {"overrides": {"typo": "GPU-3090"}})["valid"])

    def test_sequential_guides_do_not_accumulate(self):
        graph = {str(i): {"class_type": "OmniLTXStageGuide", "inputs": {"samples": [str(i-1), 0]}} for i in range(1, 4)}
        with patch("comfy_placement._estimate_component", return_value=10000):
            plan = self.plan(graph, {"mode": "single", "primary_device": "GPU-3060", "reserve_mb": 0})
        self.assertTrue(plan["valid"], plan["blockers"])
        self.assertEqual(next(d for d in plan["devices"] if d["used"])["estimated_staged_peak_mb"], 10000)

    def test_dynamic_duration_has_decode_activation_bound(self):
        graph = {
            "1": {"class_type": "OmniLTXStageDurationPredictor", "inputs": {"max_seconds": 20, "frame_rate": 24}},
            "2": {"class_type": "OmniLTXStageEmptyAVLatent", "inputs": {"width": 1024, "height": 640, "length": ["1", 0]}},
            "3": {"class_type": "OmniLTXStageAVDecode", "inputs": {"samples": ["2", 0], "temporal_size": 2048}},
        }
        components, _ = discover_components(graph)
        decode = next(c for c in components if c["component_id"] == "3:video_vae")
        self.assertGreater(decode["runtime_overhead_mb"], 20000)

    def test_host_footprint_counts_same_name_in_multiple_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for category in ("vae", "loras"):
                (root/category).mkdir()
                with (root/category/"shared.safetensors").open("wb") as f:
                    f.truncate(2*1024*1024)
            graph = {"1": {"inputs": {"vae_name": "shared.safetensors", "lora_name": "shared.safetensors"}}}
            self.assertEqual(_workflow_weight_footprint_mb(graph, root), 4)

    def test_precision_changes_admission_and_manual_unused_memory_does_not_count(self):
        self.assertEqual(estimate_model_vram_mb("qwen_omni_7b", "base", precision="fp32"), 2*estimate_model_vram_mb("qwen_omni_7b", "base", precision="bf16"))
        plan = analyze_worker_placement(model="qwen_omni_7b", variant="base", legacy_device=None, devices=DEVICES, placement={"mode": "manual", "primary_device": "GPU-3090", "eligible_devices": ["GPU-3060", "GPU-3090"], "device_map": {"": "GPU-3060"}, "reserve_mb": 0})
        self.assertEqual(plan["primary_device"], "cuda:1")
        self.assertFalse(plan["valid"])
        self.assertTrue(any("unused eligible GPUs" in b for b in plan["blockers"]))
