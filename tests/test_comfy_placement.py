"""Focused tests for Comfy workflow GPU placement and graph rewriting."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from comfy_placement import (  # noqa: E402
    apply_placement_plan,
    build_placement_plan,
    discover_components,
    instance_devices,
    normalize_policy,
)


DEVICES = [
    {
        "id": "cuda:0", "uuid": "GPU-3060", "stable_id": "GPU-3060",
        "name": "RTX 3060", "vram_total_mb": 12288, "vram_free_mb": 12000,
    },
    {
        "id": "cuda:1", "uuid": "GPU-3090", "stable_id": "GPU-3090",
        "name": "RTX 3090", "vram_total_mb": 24576, "vram_free_mb": 24000,
    },
]


def _instance():
    return types.SimpleNamespace(
        instance_id="comfy-cuda1-8188",
        pid=1234,
        device="cuda:1",
        gpu_pool=["cuda:1", "cuda:0"],
        gpu_device_map={"cuda:1": "cuda:0", "cuda:0": "cuda:1"},
        vram_mode="normal",
        startup_options={},
    )


def _h3_graph():
    return {
        "1": {"class_type": "OmniH3StageConditioning", "inputs": {
            "clip_name": "h3_text.safetensors", "device": "primary",
        }},
        "2": {"class_type": "OmniH3StageSampler", "inputs": {
            "unet_name": "h3_dit.safetensors", "device": "primary",
            "conditioning": ["1", 0], "latent": ["1", 1],
        }},
        "3": {"class_type": "OmniH3StageDecode", "inputs": {
            "samples": ["2", 0], "video_vae_name": "h3_video_vae.safetensors",
            "audio_vae_name": "h3_audio_vae.safetensors",
            "video_device": "primary", "audio_device": "primary",
        }},
    }


class PlacementPlannerTests(unittest.TestCase):
    def test_ltx_staged_conditioning_is_a_stage_one_clip_component(self):
        graph = {
            "1": {"class_type": "OmniLTXStageConditioning", "inputs": {
                "clip_name": "gemma4-ltx25.safetensors", "device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["role"], "clip")
        self.assertTrue(components[0]["staged"])
        self.assertEqual(components[0]["stage"], 1)
        self.assertEqual(components[0]["binding"], {"kind": "input", "field": "device"})

    def test_ltx_av_pipeline_has_distinct_stages_and_device_bindings(self):
        graph = {
            "1": {"class_type": "OmniLTXStageConditioning", "inputs": {
                "clip_name": "gemma4-ltx25.safetensors", "device": "primary",
            }},
            "2": {"class_type": "OmniLTXStageEmptyAVLatent", "inputs": {
                "audio_vae_name": "ltx-audio-vae.safetensors", "device": "auxiliary:1",
            }},
            "3": {"class_type": "OmniLTXStageSampler", "inputs": {
                "unet_name": "ltx25-w4a8.safetensors", "device": "primary",
            }},
            "4": {"class_type": "OmniLTXStageAVDecode", "inputs": {
                "video_vae_name": "ltx-video-vae.safetensors",
                "audio_vae_name": "ltx-audio-vae.safetensors",
                "video_device": "auxiliary:1", "audio_device": "auxiliary:1",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual([item["stage"] for item in components], [1, 2, 4, 7, 8])
        self.assertEqual(
            [item["binding"]["field"] for item in components],
            ["device", "device", "device", "video_device", "audio_device"],
        )
        self.assertTrue(all(item["staged"] for item in components))

    def test_ltx_full_temporal_decode_includes_activation_headroom(self):
        graph = {
            "1": {"class_type": "OmniLTXStageEmptyAVLatent", "inputs": {
                "audio_vae_name": "ltx-audio-vae.safetensors",
                "width": 512, "height": 320, "length": 121, "batch_size": 1,
            }},
            "2": {"class_type": "OmniLTXStageSampler", "inputs": {
                "unet_name": "ltx-model.safetensors", "latent": ["1", 0],
            }},
            "3": {"class_type": "OmniLTXStageSpatialRefine", "inputs": {
                "samples": ["2", 0],
                "upscale_model_name": "ltx-spatial-x2.safetensors",
                "video_vae_name": "ltx-video-vae.safetensors",
                "unet_name": "ltx-dev.safetensors",
            }},
            "4": {"class_type": "OmniLTXStageAVDecode", "inputs": {
                "samples": ["3", 0],
                "video_vae_name": "ltx-video-vae.safetensors",
                "audio_vae_name": "ltx-audio-vae.safetensors",
                "tile_size": 512, "temporal_size": 2048,
                "temporal_overlap": 8,
            }},
        }

        components, warnings = discover_components(graph)

        video = next(item for item in components if item["component_id"] == "4:video_vae")
        self.assertGreaterEqual(video["runtime_overhead_mb"], 8500)
        self.assertEqual(video["activation_profile"]["width"], 1024)
        self.assertEqual(video["activation_profile"]["height"], 640)
        self.assertEqual(video["activation_profile"]["active_frames"], 121)
        self.assertTrue(any("all 121 frames" in warning for warning in warnings))

    def test_ltx_full_temporal_decode_blocks_12gb_target_but_t64_fits(self):
        def graph(temporal_size):
            return {
                "1": {"class_type": "OmniLTXStageEmptyAVLatent", "inputs": {
                    "audio_vae_name": "ltx-audio-vae.safetensors",
                    "width": 512, "height": 320, "length": 121, "batch_size": 1,
                }},
                "2": {"class_type": "OmniLTXStageSampler", "inputs": {
                    "unet_name": "ltx-model.safetensors", "latent": ["1", 0],
                }},
                "3": {"class_type": "OmniLTXStageSpatialRefine", "inputs": {
                    "samples": ["2", 0],
                    "upscale_model_name": "ltx-spatial-x2.safetensors",
                    "video_vae_name": "ltx-video-vae.safetensors",
                    "unet_name": "ltx-dev.safetensors",
                }},
                "4": {"class_type": "OmniLTXStageAVDecode", "inputs": {
                    "samples": ["3", 0],
                    "video_vae_name": "ltx-video-vae.safetensors",
                    "audio_vae_name": "ltx-audio-vae.safetensors",
                    "tile_size": 512, "temporal_size": temporal_size,
                    "temporal_overlap": 8,
                }},
            }

        policy = {
            "mode": "manual",
            "eligible_devices": ["GPU-3060"],
            "primary_device": "GPU-3060",
            "reserve_mb": 1024,
            "overrides": {"4:video_vae": "GPU-3060"},
        }
        devices = [{**DEVICES[0], "vram_free_mb": 11000}]
        instance = _instance()
        instance.vram_mode = "low"
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._single_model_size_mb", return_value=1404,
        ):
            unsafe = build_placement_plan(
                graph(2048), policy, instance, devices, model_root=Path(tmp),
            )
            safe = build_placement_plan(
                graph(64), policy, instance, devices, model_root=Path(tmp),
            )

        self.assertFalse(unsafe["valid"])
        self.assertTrue(any("Activation estimate for 4:video_vae" in item for item in unsafe["blockers"]))
        self.assertTrue(safe["valid"])

    def test_ltx_duration_predictor_groups_model_and_head_on_one_stage(self):
        graph = {
            "2": {"class_type": "OmniLTXStageDurationPredictor", "inputs": {
                "unet_name": "ltx25-w4a8.safetensors",
                "duration_head_name": "ltx25-duration.safetensors",
                "device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(
            [item["component_id"] for item in components],
            ["2:model", "2:duration_head"],
        )
        self.assertEqual([item["role"] for item in components], ["model", "model_patch"])
        self.assertEqual([item["stage"] for item in components], [2, 2])
        self.assertTrue(all(item["staged"] for item in components))

    def test_ltx_guide_is_a_distinct_staged_video_vae(self):
        graph = {
            "3": {"class_type": "OmniLTXStageGuide", "inputs": {
                "video_vae_name": "ltx25-video-vae.safetensors",
                "device": "auxiliary:1",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["component_id"], "3:video_vae")
        self.assertEqual(components[0]["role"], "video_vae")
        self.assertEqual(components[0]["stage"], 3)
        self.assertEqual(components[0]["binding"], {"kind": "input", "field": "device"})

    def test_ltx_spatial_refine_groups_upscaler_and_vae_before_dev_model(self):
        graph = {
            "5": {"class_type": "OmniLTXStageSpatialRefine", "inputs": {
                "upscale_model_name": "ltx25-spatial-x2.safetensors",
                "video_vae_name": "ltx25-video-vae.safetensors",
                "unet_name": "ltx25-dev-w4a8.safetensors",
                "upscale_device": "auxiliary:1", "model_device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(
            [item["component_id"] for item in components],
            ["5:latent_upscaler", "5:video_vae", "5:model"],
        )
        self.assertEqual([item["stage"] for item in components], [5, 5, 6])
        self.assertEqual(
            [item["binding"]["field"] for item in components],
            ["upscale_device", "upscale_device", "model_device"],
        )

    def test_staged_vae_decode_keeps_upstream_loaders_resident(self):
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": "ideogram_cond.safetensors",
            }},
            "2": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": "qwen.safetensors",
            }},
            "3": {"class_type": "TestSampler", "inputs": {
                "model": ["1", 0], "clip": ["2", 0],
            }},
            "4": {"class_type": "OmniStageVAEDecode", "inputs": {
                "samples": ["3", 0], "vae_name": "flux2-vae.safetensors",
                "device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(
            [(item["role"], item["stage"], item["staged"]) for item in components],
            [("model", None, False), ("clip", None, False), ("vae", 2, True)],
        )

    def test_components_in_the_same_stage_add_to_peak(self):
        graph = {
            "1": {"class_type": "OmniH3StageFL2VConditioning", "inputs": {
                "clip_name": "qwen.safetensors",
                "video_vae_name": "video-vae.safetensors",
                "clip_device": "primary",
                "vae_device": "primary",
            }},
            "2": {"class_type": "OmniH3StageSampler", "inputs": {
                "unet_name": "h3.safetensors", "device": "primary",
                "conditioning": ["1", 0], "latent": ["1", 1],
            }},
        }
        policy = {
            "mode": "single",
            "eligible_devices": ["GPU-3090"],
            "primary_device": "GPU-3090",
            "reserve_mb": 0,
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=6000,
        ):
            plan = build_placement_plan(
                graph, policy, _instance(), DEVICES, model_root=Path(tmp),
            )

        device = next(item for item in plan["devices"] if item["stable_id"] == "GPU-3090")
        self.assertEqual(device["estimated_staged_peak_mb"], 12000)
        self.assertEqual(device["estimated_total_peak_mb"], 12000)

    def test_staged_h3_sampler_counts_turbo_lora_in_its_model_stage(self):
        graph = {
            "1": {"class_type": "OmniH3StageSampler", "inputs": {
                "unet_name": "h3.safetensors",
                "lora_name": "turbo.safetensors",
                "device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual(
            components[0]["extra_weight_refs"],
            [{"role": "lora", "name": "turbo.safetensors"}],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "diffusion_models").mkdir()
            (root / "loras").mkdir()
            (root / "diffusion_models" / "h3.safetensors").write_bytes(b"x" * 1024 * 1024)
            (root / "loras" / "turbo.safetensors").write_bytes(b"x" * 2 * 1024 * 1024)
            plan = build_placement_plan(
                graph,
                {
                    "mode": "single",
                    "eligible_devices": ["GPU-3090"],
                    "primary_device": "GPU-3090",
                    "reserve_mb": 0,
                },
                _instance(),
                DEVICES,
                model_root=root,
            )

        assignment = plan["components"][0]
        self.assertEqual(assignment["estimated_peak_mb"], 1027)

    def test_projected_fl2v_conditioning_is_a_two_device_stage(self):
        graph = {
            "1": {"class_type": "OmniH3StageFL2VConditioning", "inputs": {
                "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                "projection": "h3_qwen3vl_4b_tap24.safetensors",
                "video_vae_name": "minimax_h3_video_vae_fp16.safetensors",
                "clip_device": "auxiliary:1",
                "vae_device": "primary",
            }},
        }

        components, warnings = discover_components(graph)

        self.assertEqual(warnings, [])
        self.assertEqual([item["role"] for item in components], ["clip", "vae"])
        self.assertTrue(all(item["staged"] for item in components))
        self.assertTrue(all(item["stage"] == 1 for item in components))
        self.assertEqual(
            [item["binding"]["field"] for item in components],
            ["clip_device", "vae_device"],
        )

    def test_humo_distilled_lora_requires_lowvram_on_24gb_primary(self):
        graph = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "humo_17B_fp8_e4m3fn.safetensors"},
            },
            "2": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": ["1", 0],
                    "lora_name": "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
                },
            },
        }
        instance = _instance()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=1024,
        ):
            normal = build_placement_plan(
                graph,
                {"eligible_devices": ["GPU-3090"], "reserve_mb": 0},
                instance,
                DEVICES,
                Path(tmp),
            )
            instance.vram_mode = "low"
            low_default = build_placement_plan(
                graph,
                {"eligible_devices": ["GPU-3090"], "reserve_mb": 0},
                instance,
                DEVICES,
                Path(tmp),
            )
            instance.startup_options = {"reserve_vram": 10}
            low = build_placement_plan(
                graph,
                {"eligible_devices": ["GPU-3090"], "reserve_mb": 0},
                instance,
                DEVICES,
                Path(tmp),
            )

        self.assertFalse(normal["valid"])
        self.assertFalse(low_default["valid"])
        self.assertTrue(any("reserve_vram>=9" in item for item in normal["blockers"]))
        self.assertTrue(low["valid"])

    def test_lora_transform_is_not_an_independent_model_component(self):
        workflow = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "krea2_turbo.safetensors"},
            },
            "8": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": ["1", 0],
                    "lora_name": "krea2_style_reference.safetensors",
                    "strength_model": 1.0,
                },
            },
        }
        object_info = {
            "LoraLoaderModelOnly": {"output": ["MODEL"]},
        }

        components, _warnings = discover_components(workflow, object_info)

        self.assertEqual(
            [component["component_id"] for component in components],
            ["1:model"],
        )

    def test_audio_encoder_loader_is_an_independent_component(self):
        workflow = {
            "8": {
                "class_type": "AudioEncoderLoader",
                "inputs": {
                    "audio_encoder_name": "whisper_large_v3_fp16.safetensors",
                },
            },
            "9": {
                "class_type": "AudioEncoderEncode",
                "inputs": {"audio_encoder": ["8", 0]},
            },
        }
        object_info = {
            "AudioEncoderLoader": {"output": ["AUDIO_ENCODER"]},
        }

        components, _warnings = discover_components(workflow, object_info)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["component_id"], "8:audio_encoder")
        self.assertEqual(components[0]["model_name"], "whisper_large_v3_fp16.safetensors")
        self.assertEqual(components[0]["estimate_fraction"], 0.42)

    def test_humo_video_volume_adds_runtime_headroom(self):
        workflow = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "humo_17B_fp8_e4m3fn.safetensors"},
            },
            "12": {
                "class_type": "WanHuMoImageToVideo",
                "inputs": {"width": 480, "height": 480, "length": 49},
            },
        }

        components, _warnings = discover_components(workflow)

        self.assertEqual(components[0]["runtime_overhead_mb"], 2500)

    def test_target_instance_uses_current_free_for_warm_queue_safety(self):
        devices = [dict(DEVICES[1], vram_free_mb=4000, compute_pids=[1234])]
        instance = types.SimpleNamespace(
            pid=1234,
            device="cuda:1",
            gpu_pool=["cuda:1"],
            gpu_device_map={"cuda:1": "cuda:0"},
        )
        rows, warnings = instance_devices(
            instance, devices, normalize_policy({"reserve_mb": 1024}),
        )
        self.assertEqual(rows[0]["usable_mb"], 4000 - 1024)
        self.assertFalse(rows[0]["reusable_instance_vram"])
        self.assertTrue(rows[0]["instance_only_vram"])
        self.assertTrue(warnings)

    def test_other_compute_process_keeps_current_free_headroom(self):
        devices = [dict(
            DEVICES[1], vram_free_mb=4000, compute_pids=[1234, 5678],
        )]
        instance = types.SimpleNamespace(
            pid=1234,
            device="cuda:1",
            gpu_pool=["cuda:1"],
            gpu_device_map={"cuda:1": "cuda:0"},
        )
        rows, _warnings = instance_devices(
            instance, devices, normalize_policy({"reserve_mb": 1024}),
        )
        self.assertEqual(rows[0]["usable_mb"], 4000 - 1024)
        self.assertFalse(rows[0]["reusable_instance_vram"])

    def test_low_vram_mode_allows_only_bounded_cpu_offload(self):
        graph = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "large_model.safetensors"},
            },
        }
        devices = [dict(DEVICES[1], vram_free_mb=4000)]
        instance = _instance()
        instance.gpu_pool = ["cuda:1"]
        instance.gpu_device_map = {"cuda:1": "cuda:0"}
        instance.vram_mode = "low"
        policy = {
            "mode": "manual",
            "reserve_mb": 0,
            "overrides": {"1:model": "GPU-3090"},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=5000,
        ), mock.patch("comfy_placement._cpu_offload_budget_mb", return_value=2048):
            valid = build_placement_plan(
                graph, policy, instance, devices, Path(tmp),
            )
        self.assertTrue(valid["valid"], valid["blockers"])
        self.assertEqual(valid["summary"]["estimated_cpu_offload_mb"], 1000)
        self.assertEqual(valid["summary"]["cpu_offload_budget_mb"], 2048)
        self.assertTrue(any("CPU offload" in item for item in valid["warnings"]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=5000,
        ), mock.patch("comfy_placement._cpu_offload_budget_mb", return_value=512):
            blocked = build_placement_plan(
                graph, policy, instance, devices, Path(tmp),
            )
        self.assertFalse(blocked["valid"])
        self.assertTrue(any("workload-cgroup budget" in item for item in blocked["blockers"]))

    def test_auto_h3_uses_large_gpu_for_heavy_stages_and_small_for_vaes(self):
        estimates = {
            "1:clip": 15000,
            "2:model": 20000,
            "3:video_vae": 5000,
            "3:audio_vae": 600,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component",
            side_effect=lambda component, _root: estimates[component["component_id"]],
        ):
            plan = build_placement_plan(
                _h3_graph(), {"mode": "auto", "reserve_mb": 512},
                _instance(), DEVICES, Path(tmp),
            )
        self.assertTrue(plan["valid"], plan["blockers"])
        assigned = {item["component_id"]: item["device"]["target"] for item in plan["components"]}
        self.assertEqual(assigned["1:clip"], "primary")
        self.assertEqual(assigned["2:model"], "primary")
        self.assertEqual(assigned["3:video_vae"], "auxiliary:1")
        self.assertEqual(assigned["3:audio_vae"], "auxiliary:1")
        self.assertEqual(plan["summary"]["used_gpu_count"], 2)
        self.assertTrue(plan["summary"]["staged"])

    def test_uuid_policy_survives_physical_to_logical_mapping(self):
        policy = {
            "mode": "manual",
            "eligible_devices": ["GPU-3090", "GPU-3060"],
            "overrides": {"3:video_vae": "GPU-3060"},
            "reserve_mb": 0,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=512,
        ):
            plan = build_placement_plan(_h3_graph(), policy, _instance(), DEVICES, Path(tmp))
        row = next(item for item in plan["components"] if item["component_id"] == "3:video_vae")
        self.assertEqual(row["device"]["physical_id"], "cuda:0")
        self.assertEqual(row["device"]["target"], "auxiliary:1")
        self.assertEqual(row["reason"], "manual override")

    def test_explicit_cpu_override_is_available_in_mixed_gpu_plan(self):
        workflow = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "model.safetensors"},
            },
            "4": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "vae.safetensors"},
            },
        }
        estimates = {"1:model": 2000, "4:vae": 500}
        policy = {
            "mode": "manual",
            "allow_cpu": True,
            "overrides": {"4:vae": "cpu"},
            "reserve_mb": 512,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component",
            side_effect=lambda component, _root: estimates[component["component_id"]],
        ):
            plan = build_placement_plan(
                workflow, policy, _instance(), DEVICES, Path(tmp),
            )

        self.assertTrue(plan["valid"], plan["blockers"])
        assigned = {
            item["component_id"]: item["device"]["target"]
            for item in plan["components"]
        }
        self.assertNotEqual(assigned["1:model"], "cpu")
        self.assertEqual(assigned["4:vae"], "cpu")
        self.assertEqual(plan["summary"]["used_gpu_count"], 1)
        self.assertTrue(any(item["physical_id"] == "cpu" for item in plan["devices"]))

    def test_unseen_requested_uuid_requires_instance_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_placement_plan(
                _h3_graph(), {"eligible_devices": ["GPU-not-visible"]},
                _instance(), DEVICES, Path(tmp),
            )
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["status"], "restart-required")
        self.assertIn("Restart", " ".join(plan["blockers"]))

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "single, auto, or manual"):
            normalize_policy({"mode": "magic"})


    def test_ordinary_graph_blocks_when_all_mapped_weights_exceed_host_budget(self):
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": "base.safetensors",
            }},
            "2": {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["1", 0], "lora_name": "adapter.safetensors",
            }},
            "3": {"class_type": "ModelPatchLoader", "inputs": {
                "model": ["2", 0], "name": "talking.safetensors",
            }},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = (
                ("diffusion_models", "base.safetensors", 2),
                ("loras", "adapter.safetensors", 3),
                ("model_patches", "talking.safetensors", 4),
            )
            for category, name, size_mb in fixtures:
                path = root / category / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("wb") as handle:
                    handle.truncate(size_mb * 1024 * 1024)
            with mock.patch("comfy_placement._host_model_budget_mb", return_value=7):
                plan = build_placement_plan(
                    graph, {"mode": "auto", "reserve_mb": 512},
                    _instance(), DEVICES, root,
                )
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["summary"]["host_weight_footprint_mb"], 9)
        self.assertEqual(plan["summary"]["host_model_budget_mb"], 7)
        self.assertTrue(any("host model mapping budget" in item for item in plan["blockers"]))


class PlacementRewriteTests(unittest.TestCase):
    def test_staged_graph_inputs_are_updated_directly(self):
        graph = _h3_graph()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=512,
        ):
            plan = build_placement_plan(
                graph,
                {"mode": "manual", "overrides": {
                    "1:clip": "GPU-3090", "2:model": "GPU-3090",
                    "3:video_vae": "GPU-3060", "3:audio_vae": "GPU-3060",
                }},
                _instance(), DEVICES, Path(tmp),
            )
        patched, changes = apply_placement_plan(graph, plan)
        self.assertEqual(patched["1"]["inputs"]["device"], "primary")
        self.assertEqual(patched["2"]["inputs"]["device"], "primary")
        self.assertEqual(patched["3"]["inputs"]["video_device"], "auxiliary:1")
        self.assertEqual(patched["3"]["inputs"]["audio_device"], "auxiliary:1")
        self.assertEqual(changes["inserted_count"], 0)
        self.assertEqual(graph["3"]["inputs"]["video_device"], "primary")

    def test_standard_loader_gets_route_and_consumers_are_rewired(self):
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model.safetensors"}},
            "2": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0]}},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=1024,
        ):
            plan = build_placement_plan(
                graph, {"mode": "single", "primary_device": "GPU-3060", "reserve_mb": 0},
                _instance(), DEVICES, Path(tmp),
            )
        patched, changes = apply_placement_plan(graph, plan)
        self.assertEqual(changes["inserted_count"], 1)
        route_id = changes["inserted_node_ids"][0]
        self.assertEqual(patched[route_id]["class_type"], "OmniRouteModel")
        self.assertEqual(patched[route_id]["inputs"]["model"], ["1", 0])
        self.assertEqual(patched[route_id]["inputs"]["device"], "auxiliary:1")
        self.assertEqual(patched["2"]["inputs"]["model"], [route_id, 0])

    def test_audio_encoder_loader_gets_audio_route(self):
        graph = {
            "8": {
                "class_type": "AudioEncoderLoader",
                "inputs": {
                    "audio_encoder_name": "whisper_large_v3_fp16.safetensors",
                },
            },
            "9": {
                "class_type": "AudioEncoderEncode",
                "inputs": {"audio_encoder": ["8", 0]},
            },
        }
        object_info = {
            "AudioEncoderLoader": {"output": ["AUDIO_ENCODER"]},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "comfy_placement._estimate_component", return_value=1024,
        ):
            plan = build_placement_plan(
                graph,
                {"mode": "single", "primary_device": "GPU-3060", "reserve_mb": 0},
                _instance(),
                DEVICES,
                Path(tmp),
                object_info,
            )
        patched, changes = apply_placement_plan(graph, plan)
        route_id = changes["inserted_node_ids"][0]
        self.assertEqual(patched[route_id]["class_type"], "OmniRouteAudioEncoder")
        self.assertEqual(patched[route_id]["inputs"]["audio_encoder"], ["8", 0])
        self.assertEqual(patched["9"]["inputs"]["audio_encoder"], [route_id, 0])


if __name__ == "__main__":
    unittest.main()
