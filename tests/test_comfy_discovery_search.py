"""Focused contracts for searchable workflows, extensions, and models."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import extensions, registry, workflows  # noqa: E402


def test_saved_workflow_search_matches_notes_nodes_and_tags(monkeypatch, tmp_path):
    monkeypatch.setattr(workflows, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(workflows, "_METADATA_DIR", tmp_path / ".metadata")
    graph = {
        "1": {
            "class_type": "MarkdownNote",
            "inputs": {"text": "portrait workflow with copper lighting"},
            "_meta": {"title": "Lighting notes"},
        },
        "2": {"class_type": "KSampler", "inputs": {"seed": 4}},
    }
    (tmp_path / "portrait.json").write_text(json.dumps(graph), encoding="utf-8")
    workflows._write_metadata(
        "portrait.json", ["portrait", "production"], "studio portrait pipeline",
    )

    note_result = asyncio.run(workflows.search_workflows(
        q="copper", tags="", scope="saved", limit=20,
    ))
    assert note_result["total"] == 1
    assert "notes" in note_result["results"][0]["matched_fields"]
    assert "MarkdownNote" in note_result["results"][0]["node_classes"]

    tag_result = asyncio.run(workflows.search_workflows(
        q="", tags="portrait", scope="saved", limit=20,
    ))
    assert tag_result["results"][0]["tags"] == ["portrait", "production"]


def test_template_search_skips_indexes_and_labels_remote_api(monkeypatch, tmp_path):
    remote = tmp_path / "api_krea2_t2i.json"
    remote.write_text(json.dumps({
        "1": {"class_type": "Krea2ImageNode", "inputs": {"prompt": "cat"}},
    }), encoding="utf-8")
    index = tmp_path / "index.zh-CN.json"
    index.write_text(json.dumps({"krea": "translated catalog label"}), encoding="utf-8")
    monkeypatch.setattr("routers.assets._template_files", lambda: [
        {
            "source": "package-template", "path": remote,
            "template_id": remote.stem, "package": "templates-json",
            "package_version": "1.0",
        },
        {
            "source": "package-template", "path": index,
            "template_id": index.stem, "package": "templates-json",
            "package_version": "1.0",
        },
    ])

    result = asyncio.run(workflows.search_workflows(
        q="krea", tags="", scope="templates", limit=20,
    ))
    assert result["total"] == 1
    assert result["results"][0]["execution_mode"] == "remote-api"
    assert result["results"][0]["graph_format"] == "api"
    assert result["results"][0]["api_runnable"] is True
    assert result["results"][0]["requires_api_export"] is False
    assert result["skipped"] == 1


def test_live_template_can_be_inspected_with_provenance(monkeypatch, tmp_path):
    path = tmp_path / "krea_local.json"
    graph = {"1": {"class_type": "UNETLoader", "inputs": {}}}
    path.write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setattr("routers.assets._template_files", lambda: [{
        "source": "package-template", "path": path,
        "template_id": path.stem, "package": "templates-json",
        "package_version": "1.2",
    }])

    result = asyncio.run(workflows.get_workflow_template(
        "krea_local", source="package-template", package="templates-json",
    ))
    assert result["template"]["execution_mode"] == "local"
    assert result["template"]["package_version"] == "1.2"
    assert result["workflow"] == graph


def test_live_template_selector_accepts_catalog_punctuation_but_not_paths(
    monkeypatch, tmp_path,
):
    path = tmp_path / "Image to Video (LTX-2.3).json"
    graph = {"1": {"class_type": "LTXVImgToVideoInplace", "inputs": {}}}
    path.write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setattr("routers.assets._template_files", lambda: [{
        "source": "blueprint", "path": path,
        "template_id": path.stem, "package": "", "package_version": "",
    }])

    result = asyncio.run(workflows.get_workflow_template(
        path.stem, source="blueprint", package="",
    ))
    assert result["template"]["id"] == "Image to Video (LTX-2.3)"
    assert result["workflow"] == graph

    for invalid in ("../escape", "nested/name", "bad\\name", "line\nbreak"):
        try:
            asyncio.run(workflows.get_workflow_template(invalid))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 400
        else:
            raise AssertionError(f"unsafe template id accepted: {invalid!r}")


def test_extension_mapping_exposes_provided_node_classes():
    entry = {
        "id": "impact",
        "title": "Impact Pack",
        "files": ["https://github.com/example/impact.git"],
    }
    mappings = {
        "https://github.com/example/impact": [
            ["FaceDetailer", "SAMLoader"], {"title_aux": "Impact nodes"},
        ],
    }
    row = registry._node_record(entry, mappings)
    assert row["provided_nodes"] == ["FaceDetailer", "SAMLoader"]
    assert row["mapping_title"] == "Impact nodes"
    assert row["catalog_key"] == "impact"
    assert row["install"]["path"] == "/api/registry/comfy/nodes/impact/install"


def test_catalog_snapshot_sorts_and_embeds_revision_in_install_action(monkeypatch):
    async def node_list():
        return {
            "data": {"custom_nodes": [
                {"id": "b", "title": "Beta", "files": ["https://example.invalid/b"]},
                {"id": "a", "title": "Alpha", "files": ["https://example.invalid/a"]},
            ]},
            "fetched_at": 100.0,
            "error": None,
            "source": "test-list",
        }

    async def node_map():
        return {
            "data": {},
            "fetched_at": 100.0,
            "error": None,
            "source": "test-map",
        }

    monkeypatch.setattr(registry, "_fetch_manager_list_async", node_list)
    monkeypatch.setattr(registry, "_fetch_manager_map_async", node_map)
    snapshot = asyncio.run(registry._manager_catalog_snapshot())

    assert [row["id"] for row in snapshot["records"]] == ["a", "b"]
    assert all(
        row["install"]["body"]["catalog_revision"] == snapshot["revision"]
        for row in snapshot["records"]
    )
    assert snapshot["source"] == "test-list"


def test_local_manager_catalog_reader_supports_offline_fallback(monkeypatch, tmp_path):
    payload = {"custom_nodes": [{"id": "offline-node"}]}
    (tmp_path / "custom-node-list.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_MANAGER_LOCAL_DIR", tmp_path)
    assert registry._read_local_manager_catalog("custom-node-list.json") == payload


def test_manager_node_search_has_stable_gap_free_pagination(monkeypatch):
    records = [
        registry._node_record({
            "id": node_id,
            "title": title,
            "files": [f"https://github.com/example/{node_id}"],
        })
        for node_id, title in (("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"))
    ]

    async def snapshot(*, refresh=False):
        return {
            "records": records,
            "revision": "revision-1",
            "fetched_at": 100.0,
            "warning": None,
        }

    monkeypatch.setattr(registry, "_manager_catalog_snapshot", snapshot)
    first = asyncio.run(registry.search_comfy_nodes(
        q="", limit=2, offset=0, catalog_revision=None, refresh=False,
    ))
    second = asyncio.run(registry.search_comfy_nodes(
        q="", limit=2, offset=first["next_offset"],
        catalog_revision=first["catalog_revision"], refresh=False,
    ))

    assert [row["id"] for row in first["nodes"]] == ["a", "b"]
    assert [row["id"] for row in second["nodes"]] == ["c"]
    assert first["catalog_total"] == first["total"] == 3
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert second["next_offset"] is None


def test_manager_node_search_rejects_changed_catalog_revision(monkeypatch):
    async def snapshot(*, refresh=False):
        return {
            "records": [],
            "revision": "revision-new",
            "fetched_at": 100.0,
            "warning": None,
        }

    monkeypatch.setattr(registry, "_manager_catalog_snapshot", snapshot)
    try:
        asyncio.run(registry.search_comfy_nodes(
            q="", limit=50, offset=50,
            catalog_revision="revision-old", refresh=False,
        ))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
    else:
        raise AssertionError("changed catalog revision was accepted")


def test_catalog_install_delegates_expected_nodes_to_guarded_lifecycle(monkeypatch):
    record = registry._node_record({
        "id": "impact",
        "title": "Impact Pack",
        "files": ["https://github.com/example/impact.git"],
    }, {
        "https://github.com/example/impact": [
            ["FaceDetailer", "SAMLoader"], {"title_aux": "Impact nodes"},
        ],
    })

    async def snapshot(*, refresh=False):
        return {
            "records": [record],
            "revision": "revision-1",
            "fetched_at": 100.0,
            "warning": None,
        }

    captured = {}

    async def manage(request):
        captured["request"] = request
        return {"status": "running", "job_id": "job-7"}

    monkeypatch.setattr(registry, "_manager_catalog_snapshot", snapshot)
    monkeypatch.setattr(extensions, "manage_extension", manage)
    result = asyncio.run(registry.install_comfy_catalog_node(
        "impact",
        registry.InstallCatalogNodeRequest(catalog_revision="revision-1"),
    ))

    assert result["expected_node_count"] == 2
    assert result["job_status_path"] == "/api/jobs/job-7"
    assert captured["request"].repo_url == "https://github.com/example/impact.git"
    assert captured["request"].expected_nodes == ["FaceDetailer", "SAMLoader"]


def test_manager_and_installed_model_searches_remain_source_labeled(monkeypatch):
    monkeypatch.setattr(registry, "_manager_model_rows", lambda: [{
        "name": "TAESD Decoder",
        "filename": "taesd_decoder.pth",
        "save_path": "vae_approx",
        "type": "TAESD",
        "base": "SD1.5",
        "description": "Fast previews",
        "url": "https://example.invalid/model.pth",
    }])
    monkeypatch.setattr(
        registry, "_installed_model_names", lambda: {"vae_approx": {"taesd_decoder.pth"}},
    )
    manager = asyncio.run(registry.search_manager_models(
        q="preview", category=None, limit=20,
    ))
    assert manager["models"][0]["source"] == "comfyui-manager"
    assert manager["models"][0]["installed"] is True
    assert manager["models"][0]["install_selector"]["save_path"] == "vae_approx"

    monkeypatch.setattr(registry.comfy_manager, "get_installed_models", lambda: {
        "checkpoints": [{"name": "portrait.safetensors", "size_mb": 12.0}],
    })
    installed = asyncio.run(registry.search_installed_models(
        q="portrait", category=None, limit=20,
    ))
    assert installed["models"][0]["source"] == "installed-filesystem"


def test_manager_nested_category_detects_existing_root_model(monkeypatch):
    monkeypatch.setattr(registry, "_manager_model_rows", lambda: [{
        "name": "SDXL Base",
        "filename": "sd_xl_base_1.0.safetensors",
        "save_path": "checkpoints/SDXL",
        "type": "checkpoint",
        "base": "SDXL",
        "url": "https://example.invalid/sd_xl_base_1.0.safetensors",
    }])
    monkeypatch.setattr(registry, "_installed_model_names", lambda: {
        "checkpoints": {"sd_xl_base_1.0.safetensors"},
    })

    result = asyncio.run(registry.search_manager_models(
        q="SDXL Base", category="checkpoints", limit=20,
    ))

    assert result["models"][0]["installed"] is True
    assert result["models"][0]["category"] == "checkpoints/SDXL"
    assert result["models"][0]["category_root"] == "checkpoints"
