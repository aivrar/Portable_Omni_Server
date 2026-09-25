"""ComfyUI blueprint asset scanner regression tests."""

import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import assets  # noqa: E402


class BlueprintAssetScanTests(unittest.TestCase):
    def setUp(self):
        assets.invalidate_template_cache()
        self.addCleanup(assets.invalidate_template_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.old_comfyui = assets.COMFYUI_DIR
        self.old_workflows = assets.WORKFLOWS_DIR
        assets.COMFYUI_DIR = self.base / "comfyui"
        assets.WORKFLOWS_DIR = self.base / "workflows"
        (assets.COMFYUI_DIR / "blueprints").mkdir(parents=True)
        (assets.COMFYUI_DIR / "models" / "vae").mkdir(parents=True)
        assets.WORKFLOWS_DIR.mkdir(parents=True)

    def tearDown(self):
        assets.COMFYUI_DIR = self.old_comfyui
        assets.WORKFLOWS_DIR = self.old_workflows
        self.tmp.cleanup()

    def test_scans_nested_models_and_marks_installed_assets(self):
        (assets.COMFYUI_DIR / "models" / "vae" / "have.safetensors").write_bytes(b"x")
        blueprint = {
            "nodes": [
                {
                    "models": [
                        {
                            "name": "have.safetensors",
                            "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/vae/have.safetensors",
                            "directory": "vae",
                        },
                        {
                            "name": "future.safetensors",
                            "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/latent/future.safetensors",
                            "directory": "latent_upscale_models",
                        },
                        {
                            "name": "ignored.safetensors",
                            "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/ignored.safetensors",
                            "directory": "not_a_comfy_category",
                        },
                    ]
                }
            ]
        }
        (assets.COMFYUI_DIR / "blueprints" / "Text to Image.json").write_text(
            json.dumps(blueprint),
            encoding="utf-8",
        )

        report = assets._scan_blueprint_requirements("Text to Image.json")

        self.assertEqual(report["total_blueprints"], 1)
        self.assertEqual(report["total_models"], 2)
        self.assertEqual(report["total_missing"], 1)
        self.assertEqual(report["missing"][0]["category"], "latent_upscale_models")
        self.assertTrue(next(item for item in report["models"] if item["name"] == "have.safetensors")["installed"])
        missing = report["missing"][0]
        self.assertEqual(
            missing["target_path"],
            str(assets.COMFYUI_DIR / "models" / "latent_upscale_models" / "future.safetensors"),
        )
        self.assertEqual(missing["install"]["transport"], "huggingface-hub+xet")
        self.assertEqual(report["storage"]["model_root"], str(assets.COMFYUI_DIR / "models"))

    def test_shared_legacy_model_tree_is_not_scanned(self):
        legacy = self.base / "models" / "comfyui" / "vae"
        legacy.mkdir(parents=True)
        (legacy / "legacy.safetensors").write_bytes(b"x")

        self.assertFalse(assets._asset_installed("vae", "legacy.safetensors"))

    def test_exact_nested_model_delete_stays_in_comfy_tree(self):
        target = assets.COMFYUI_DIR / "models" / "checkpoints" / "family" / "model.safetensors"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"weights")

        result = asyncio.run(assets.delete_asset("checkpoints", "family/model.safetensors"))

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["path"], str(target))
        self.assertEqual(result["size_bytes"], 7)
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertEqual(result["pruned_directories"], [str(target.parent)])

    def test_model_delete_rejects_category_escape(self):
        outside = assets.COMFYUI_DIR / "models" / "outside.safetensors"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"keep")

        with self.assertRaises(assets.HTTPException):
            asyncio.run(assets.delete_asset("checkpoints", "../outside.safetensors"))

        self.assertEqual(outside.read_bytes(), b"keep")

    def test_accepts_new_native_comfy_model_categories(self):
        blueprint = {
            "nodes": [{
                "models": [{
                    "name": "birefnet.safetensors",
                    "url": "https://huggingface.co/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors",
                    "directory": "background_removal",
                }]
            }]
        }
        (assets.COMFYUI_DIR / "blueprints" / "Remove Background.json").write_text(
            json.dumps(blueprint),
            encoding="utf-8",
        )

        report = assets._scan_blueprint_requirements("Remove Background.json")

        self.assertEqual(report["total_models"], 1)
        self.assertEqual(report["models"][0]["category"], "background_removal")
        self.assertTrue(report["models"][0]["downloadable"])
        self.assertEqual(report["total_downloadable_missing"], 1)

    def test_extracts_huggingface_links_from_notes(self):
        blueprint = {
            "nodes": [{
                "type": "Note",
                "widgets_values": [
                    "Download https://huggingface.co/Comfy-Org/Upscalers/resolve/main/upscale_models/test-upscale.pth"
                ],
            }]
        }
        (assets.COMFYUI_DIR / "blueprints" / "Note Link.json").write_text(
            json.dumps(blueprint),
            encoding="utf-8",
        )

        report = assets._scan_blueprint_requirements("Note Link.json")

        self.assertEqual(report["total_models"], 1)
        model = report["models"][0]
        self.assertEqual(model["category"], "upscale_models")
        self.assertEqual(model["name"], "test-upscale.pth")
        self.assertIn("link", model["sources"])
        self.assertTrue(model["downloadable"])

    def test_infers_workflow_model_references_without_download_url(self):
        workflow = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "example.safetensors"},
            },
            "2": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "vae-ft-mse.safetensors"},
            },
        }
        (assets.COMFYUI_DIR / "blueprints" / "Workflow Only.json").write_text(
            json.dumps(workflow),
            encoding="utf-8",
        )

        report = assets._scan_blueprint_requirements("Workflow Only.json")

        by_name = {m["name"]: m for m in report["models"]}
        self.assertEqual(by_name["example.safetensors"]["category"], "checkpoints")
        self.assertEqual(by_name["vae-ft-mse.safetensors"]["category"], "vae")
        self.assertFalse(by_name["example.safetensors"]["downloadable"])
        self.assertEqual(report["total_undownloadable"], 2)

    def test_filters_batch_pending_by_category_and_template(self):
        req = assets.InstallBlueprintAssetsRequest(
            categories=["vae"],
            templates=["Keep.json"],
            limit=10,
        )
        base = [
            {
                "name": "keep.safetensors",
                "category": "vae",
                "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/vae/keep.safetensors",
                "downloadable": True,
                "templates": ["Keep.json"],
            },
            {
                "name": "wrong-template.safetensors",
                "category": "vae",
                "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/vae/wrong-template.safetensors",
                "downloadable": True,
                "templates": ["Other.json"],
            },
            {
                "name": "wrong-category.safetensors",
                "category": "checkpoints",
                "url": "https://huggingface.co/Comfy-Org/Test/resolve/main/checkpoints/wrong-category.safetensors",
                "downloadable": True,
                "templates": ["Keep.json"],
            },
        ]

        pending = assets._filter_blueprint_pending(base, req)

        self.assertEqual([item["name"] for item in pending], ["keep.safetensors"])

    def test_rejects_unknown_batch_category(self):
        req = assets.InstallBlueprintAssetsRequest(categories=["not_a_real_category"])

        with self.assertRaises(assets.HTTPException):
            assets._filter_blueprint_pending([], req)

    def test_discovers_versioned_default_template_packages_dynamically(self):
        package_root = self.base / "site-packages"
        template_rel = Path("comfyui_workflow_templates_media_image/templates/live-default.json")
        template_path = package_root / template_rel
        template_path.parent.mkdir(parents=True)
        template_path.write_text(json.dumps({
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "dynamic-default.safetensors"},
            }
        }), encoding="utf-8")

        class FakeDistribution:
            metadata = {"Name": "comfyui-workflow-templates-media-image"}
            version = "9.8.7"
            files = [template_rel]

            @staticmethod
            def locate_file(relative):
                return package_root / relative

        with mock.patch.object(
            assets.importlib.metadata,
            "distributions",
            return_value=[FakeDistribution()],
        ):
            report = assets._scan_blueprint_requirements("live-default")

        self.assertEqual(report["total_templates"], 1)
        self.assertEqual(report["source_counts"], {"package-template": 1})
        self.assertEqual(report["template_packages"], [{
            "name": "comfyui-workflow-templates-media-image",
            "version": "9.8.7",
        }])
        self.assertEqual(report["models"][0]["name"], "dynamic-default.safetensors")
        self.assertEqual(report["models"][0]["category"], "checkpoints")

    def test_template_inventory_summary_skips_workflow_parsing(self):
        package_root = self.base / "site-packages"
        template_rel = Path("comfyui_workflow_templates_json/templates/live.json")
        template_path = package_root / template_rel
        template_path.parent.mkdir(parents=True)
        template_path.write_text("not parsed by summary", encoding="utf-8")

        class FakeDistribution:
            metadata = {"Name": "comfyui-workflow-templates-json"}
            version = "1.2.3"
            files = [template_rel]

            @staticmethod
            def locate_file(relative):
                return package_root / relative

        with mock.patch.object(
            assets.importlib.metadata,
            "distributions",
            return_value=[FakeDistribution()],
        ):
            report = assets._template_inventory_summary(include_templates=True)

        self.assertTrue(report["summary_only"])
        self.assertEqual(report["total_templates"], 1)
        self.assertEqual(report["source_counts"], {"package-template": 1})
        self.assertEqual(report["templates"][0]["id"], "live")
        self.assertEqual(report["template_packages"], [{
            "name": "comfyui-workflow-templates-json",
            "version": "1.2.3",
        }])


if __name__ == "__main__":
    unittest.main()
