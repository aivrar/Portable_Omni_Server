"""Prevent code-only updates from reintroducing model migration on startup."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


def test_linux_entrypoints_have_lf_line_endings_on_disk():
    # Text-mode reads normalize CRLF and concealed a real packaged WSL failure.
    for path in [*SERVER.rglob("*.sh"), ROOT / "omni-cli"]:
        assert b"\r" not in path.read_bytes(), f"Bash entrypoint must use LF: {path}"


def test_manifest_tracks_the_isolated_comfy_model_store_revision():
    manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
    assert "OMNI_SETUP_REV=20260925_audit_repairs" in manifest["setup"]
    assert "snapshot_storage" not in manifest["setup"]
    assert manifest["snapshot"] is False


def test_setup_keeps_public_paths_inside_the_distro_and_persists_state():
    setup = (SERVER / "setup.sh").read_text(encoding="utf-8")
    assert 'MODELS_DIR="$OPT_DIR/models"' in setup
    assert 'OUTPUT_DIR="$OPT_DIR/output"' in setup
    assert 'WORKFLOWS_DIR="$OPT_DIR/workflows"' in setup
    assert 'PERSIST_DIR="${OMNI_PERSIST_DIR:-/var/lib/omni_studio}"' in setup
    assert "ensure_persistent_link" in setup
    assert 'ensure_persistent_link "$MODELS_DIR" "$PERSIST_DIR/models"' in setup
    assert 'ensure_persistent_link "$OUTPUT_DIR" "$PERSIST_DIR/output"' in setup
    assert 'ensure_persistent_link "$WORKFLOWS_DIR" "$PERSIST_DIR/workflows"' in setup
    assert 'ensure_persistent_link "$COMFYUI_DIR/models"' in setup
    assert 'ensure_persistent_link "$COMFYUI_DIR/input"' in setup
    assert 'ensure_persistent_link "$COMFYUI_DIR/user"' in setup
    assert 'rm -rf "$live_path"' not in setup
    assert "OMNI_PERSIST_DIR:-/mnt/" not in setup
    assert "Cleaning rebuildable caches before environment snapshot" not in setup
    assert "clean_rebuildable_caches" not in setup
    assert 'mkdir -p "$COMFYUI_DIR/models/$dir"' in setup
    assert 'base_path: $MODELS_DIR/comfyui' not in setup


def test_runtime_uses_canonical_comfy_path_without_dynamic_recovery_logic():
    manager = (SERVER / "comfy_manager.py").read_text(encoding="utf-8")
    config = (SERVER / "config.py").read_text(encoding="utf-8")
    extensions = (SERVER / "routers" / "extensions.py").read_text(encoding="utf-8")

    assert "models/comfyui" not in manager
    assert "copytree(legacy" not in manager
    assert "symlink_to" not in manager
    assert "PERSIST_ROOT" not in config
    assert 'str(APP_DIR / "models")' in config
    assert 'models_root = (COMFYUI_DIR / "models").resolve()' in extensions
    assert 'COMFYUI_MODELS_DIR = COMFYUI_DIR / "models"' in config
    assert 'cmd.extend(["--extra-model-paths-config"' not in manager


def test_setup_keeps_runtime_agent_guidance_in_sync():
    setup = (SERVER / "setup.sh").read_text(encoding="utf-8")
    assert 'sync_runtime_tree "docs"' in setup
    assert 'sync_runtime_tree "skills"' in setup
    assert 'sync_runtime_tree "cli"' in setup
    assert 'cp "$SOURCE_APP_DIR/omni-cli" "$OPT_DIR/omni-cli"' in setup
    assert 'ln -s "$OPT_DIR/omni-cli" /usr/local/bin/omni-cli' in setup
    assert 'cp "$SOURCE_APP_DIR/AGENTS.md" "$OPT_DIR/AGENTS.md"' in setup


def test_cli_shims_resolve_the_runtime_package_from_any_working_directory():
    linux_shim = (ROOT / "omni-cli").read_text(encoding="utf-8")
    windows_shim = (ROOT / "omni-cli.bat").read_text(encoding="utf-8")
    assert 'export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"' in linux_shim
    assert "PYTHONPATH=/opt/omni_studio" in windows_shim


def test_full_shutdown_code_is_still_shipped():
    bridge = (ROOT / "bridge.py").read_text(encoding="utf-8")
    shutdown = (SERVER / "omni_shutdown.py").read_text(encoding="utf-8")
    assert '"/api/app/shutdown"' in bridge
    assert "_begin_full_app_shutdown" in bridge
    assert "def sweep_all(" in shutdown
