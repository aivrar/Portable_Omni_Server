"""Fast, offline workflow-readiness checks."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import workflows  # noqa: E402
from config import COMFYUI_MODEL_CATEGORIES  # noqa: E402


def _inventory(**entries):
    result = {}
    for category in COMFYUI_MODEL_CATEGORIES:
        names = {str(item).casefold() for item in entries.get(category, [])}
        result[category] = {
            "relative": set(names),
            "basenames": {Path(item).name for item in names},
        }
    return result


class WorkflowRequirementTests(unittest.TestCase):
    def test_invalid_placement_is_a_top_level_hard_stop(self):
        report = {
            "readiness": "ready",
            "ready_to_run": True,
            "placement_plan": {"valid": False, "blockers": ["too large"]},
        }

        workflows._apply_placement_readiness(report)

        self.assertEqual(report["readiness"], "needs-placement")
        self.assertFalse(report["ready_to_run"])

    def test_clip_projection_category_is_managed(self):
        self.assertIn("clip_projections", COMFYUI_MODEL_CATEGORIES)

    def test_clip_projection_input_uses_its_registered_model_category(self):
        graph = {
            "1": {"class_type": "ClipProjLoader", "inputs": {
                "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                "projection": "h3_qwen3vl_4b_tap24.safetensors",
            }},
        }
        inventory = _inventory(
            text_encoders=["qwen3vl_4b_fp8_scaled.safetensors"],
            clip_projections=["h3_qwen3vl_4b_tap24.safetensors"],
        )

        report = workflows._analyze_workflow_sync(
            graph,
            "clipproj.json",
            None,
            inventory=inventory,
            manager_rows=[],
        )

        by_input = {
            item["references"][0]["field"]: item
            for item in report["models"]["items"]
        }
        self.assertEqual(by_input["projection"]["category"], "clip_projections")
        self.assertTrue(by_input["projection"]["installed"])

    def test_omni_ltx_spatial_refiner_uses_latent_upscale_category(self):
        graph = {
            "1": {"class_type": "OmniLTXStageSpatialRefine", "inputs": {
                "upscale_model_name": "ltx25-spatial-x2.safetensors",
            }},
        }
        inventory = _inventory(
            latent_upscale_models=["ltx25-spatial-x2.safetensors"],
        )

        report = workflows._analyze_workflow_sync(
            graph, "ltx-refine.json", None, inventory=inventory, manager_rows=[],
        )

        item = report["models"]["items"][0]
        self.assertEqual(item["category"], "latent_upscale_models")
        self.assertTrue(item["installed"])

    def test_omni_ltx_duration_head_uses_model_patch_category(self):
        graph = {
            "1": {"class_type": "OmniLTXStageDurationPredictor", "inputs": {
                "duration_head_name": "ltx25-duration.safetensors",
            }},
        }
        inventory = _inventory(model_patches=["ltx25-duration.safetensors"])

        report = workflows._analyze_workflow_sync(
            graph, "ltx-duration.json", None, inventory=inventory, manager_rows=[],
        )

        item = report["models"]["items"][0]
        self.assertEqual(item["category"], "model_patches")
        self.assertTrue(item["installed"])

    def test_krea_raw_rejects_turbo_sampling_semantics(self):
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": "krea2_raw_int8_convrot.safetensors",
            }},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "scene"}},
            "4": {"class_type": "ConditioningZeroOut", "inputs": {
                "conditioning": ["3", 0],
            }},
            "5": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 1024, "height": 1024,
            }},
            "6": {"class_type": "KSampler", "inputs": {"negative": ["4", 0]}},
        }
        issues = workflows._krea_raw_semantic_issues(graph)
        self.assertEqual(len(issues), 2)

    def test_krea_raw_accepts_corrected_shift_and_empty_unconditional(self):
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": "krea2_raw_int8_convrot.safetensors",
            }},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 1024, "height": 1024,
            }},
            "6": {"class_type": "KSampler", "inputs": {"negative": ["4", 0]}},
            "13": {"class_type": "ModelSamplingFlux", "inputs": {
                "base_shift": 0.5, "max_shift": 0.90625,
                "width": 1024, "height": 1024,
            }},
        }
        self.assertEqual(workflows._krea_raw_semantic_issues(graph), [])

    def test_resolves_exact_manager_model_without_network(self):
        graph = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "wanted.safetensors"},
            }
        }
        object_info = {"CheckpointLoaderSimple": {"input": {"required": {}}}}
        rows = [{
            "name": "Wanted",
            "filename": "wanted.safetensors",
            "save_path": "checkpoints",
            "url": "https://huggingface.co/org/repo/resolve/main/wanted.safetensors",
        }]

        report = workflows._analyze_workflow_sync(
            graph,
            "wanted.json",
            object_info,
            inventory=_inventory(),
            manager_rows=rows,
        )

        self.assertEqual(report["readiness"], "needs-models")
        self.assertEqual(report["models"]["downloadable_count"], 1)
        self.assertEqual(report["models"]["missing"][0]["source"], "workflow,comfyui-manager")
        self.assertEqual(
            report["models"]["missing"][0]["install"]["path"],
            "/api/assets/comfy/checkpoints/install-url",
        )
        model = report["models"]["missing"][0]
        self.assertEqual(model["install"]["method"], "POST")
        self.assertEqual(model["install"]["transport"], "huggingface-hub+xet")
        self.assertTrue(model["target_path"].replace("\\", "/").endswith(
            "/comfyui/models/checkpoints/wanted.safetensors"
        ))
        self.assertEqual(
            model["delete"]["path"],
            "/api/assets/comfy/checkpoints/wanted.safetensors",
        )
        self.assertEqual(report["storage"]["mode"], "comfyui-native")

    def test_preserves_nested_workflow_model_destination(self):
        graph = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "family/wanted.safetensors"},
            },
            "note": "https://huggingface.co/org/repo/resolve/main/wanted.safetensors",
        }

        report = workflows._analyze_workflow_sync(
            graph, "nested.json", None,
            inventory=_inventory(), manager_rows=[],
        )

        model = report["models"]["items"][0]
        self.assertEqual(model["install"]["body"]["name"], "family/wanted.safetensors")
        self.assertTrue(model["target_path"].replace("\\", "/").endswith(
            "/comfyui/models/checkpoints/family/wanted.safetensors"
        ))

    def test_live_schema_infers_custom_model_field(self):
        graph = {
            "9": {
                "class_type": "ThirdPartyLoader",
                "inputs": {"weights_choice": "future.gguf"},
            }
        }
        object_info = {
            "ThirdPartyLoader": {
                "input": {
                    "required": {
                        "weights_choice": [["installed.gguf"], {}],
                    }
                }
            }
        }
        inventory = _inventory(diffusion_models=["installed.gguf"])

        report = workflows._analyze_workflow_sync(
            graph, "custom.json", object_info,
            inventory=inventory, manager_rows=[],
        )

        self.assertEqual(report["models"]["items"][0]["category"], "diffusion_models")
        self.assertEqual(report["models"]["unclassified_inputs"], [])
        self.assertEqual(report["nodes"]["missing_count"], 0)

    def test_audio_encoder_loader_uses_audio_encoder_category(self):
        graph = {
            "8": {
                "class_type": "AudioEncoderLoader",
                "inputs": {
                    "audio_encoder_name": "whisper_large_v3_fp16.safetensors",
                },
            }
        }
        object_info = {
            "AudioEncoderLoader": {
                "input": {
                    "required": {
                        "audio_encoder_name": [
                            ["whisper_large_v3_fp16.safetensors"],
                            {},
                        ],
                    }
                }
            }
        }

        report = workflows._analyze_workflow_sync(
            graph,
            "humo.json",
            object_info,
            inventory=_inventory(
                audio_encoders=["whisper_large_v3_fp16.safetensors"],
            ),
            manager_rows=[],
        )

        self.assertEqual(report["models"]["items"][0]["category"], "audio_encoders")
        self.assertEqual(report["models"]["unclassified_inputs"], [])
        self.assertEqual(report["readiness"], "ready")

    def test_dynamic_combo_artifact_loader_uses_installed_category(self):
        graph = {
            "4": {
                "class_type": "LTXVLoadConditioning",
                "inputs": {"file_name": "ltx25-positive.safetensors"},
            }
        }
        object_info = {
            "LTXVLoadConditioning": {
                "input": {
                    "required": {
                        "file_name": [
                            "COMBO",
                            {"options": ["ltx25-positive.safetensors"]},
                        ],
                    }
                }
            }
        }

        report = workflows._analyze_workflow_sync(
            graph,
            "ltx25-stage.json",
            object_info,
            inventory=_inventory(embeddings=["ltx25-positive.safetensors"]),
            manager_rows=[],
        )

        self.assertEqual(report["models"]["items"][0]["category"], "embeddings")
        self.assertEqual(report["models"]["unclassified_inputs"], [])
        self.assertEqual(report["readiness"], "ready")

    def test_unknown_custom_field_exposes_cross_category_exact_matches(self):
        graph = {
            "4": {
                "class_type": "ThirdPartyUniversalLoader",
                "inputs": {"weights_choice": "future-model.safetensors"},
            }
        }
        rows = [{
            "name": "Future Model",
            "filename": "future-model.safetensors",
            "save_path": "checkpoints/experimental",
            "url": "https://huggingface.co/org/repo/resolve/main/future-model.safetensors",
        }]

        report = workflows._analyze_workflow_sync(
            graph, "future.json", None,
            inventory=_inventory(diffusion_models=["future-model.safetensors"]),
            manager_rows=rows,
        )

        unknown = report["models"]["unclassified_inputs"][0]
        self.assertEqual(report["readiness"], "needs-review")
        self.assertEqual(unknown["installed_matches"][0]["category"], "diffusion_models")
        self.assertEqual(unknown["manager_candidates"][0]["category"], "checkpoints")
        self.assertEqual(unknown["manager_candidates"][0]["save_path"], "checkpoints/experimental")
        self.assertEqual(
            unknown["search"]["manager_path"],
            "/api/registry/comfy/manager-models/search",
        )

    def test_ui_graph_is_not_reported_as_api_ready(self):
        report = workflows._analyze_workflow_sync(
            {"nodes": [{"type": "SaveImage", "inputs": []}]},
            "ui.json",
            {"SaveImage": {"input": {}}},
            inventory=_inventory(),
            manager_rows=[],
        )

        self.assertEqual(report["readiness"], "needs-api-export")
        self.assertFalse(report["ready_to_run"])

    def test_ui_notes_and_embedded_subgraphs_are_not_missing_custom_nodes(self):
        subgraph_id = "4c314f31-ecda-4b08-ae98-faaba1bf613f"
        graph = {"nodes": [
            {"type": "SaveVideo", "inputs": [{"name": "video"}], "outputs": []},
            {"type": subgraph_id, "inputs": [{"name": "prompt"}], "outputs": [{"name": "VIDEO"}]},
            {"type": "MarkdownNote", "inputs": [], "outputs": []},
        ]}

        report = workflows._analyze_workflow_sync(
            graph, "ui-subgraph.json", {"SaveVideo": {"input": {}}},
            inventory=_inventory(), manager_rows=[],
        )

        by_type = {item["class_type"]: item["status"] for item in report["nodes"]["items"]}
        self.assertEqual(by_type["SaveVideo"], "installed")
        self.assertEqual(by_type[subgraph_id], "embedded-subgraph")
        self.assertEqual(by_type["MarkdownNote"], "ui-only")
        self.assertEqual(report["nodes"]["missing_count"], 0)
        self.assertEqual(report["nodes"]["runtime_total"], 1)


if __name__ == "__main__":
    unittest.main()
