"""Regression tests for the shell model installer."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "server" / "install_model.sh"


def _extract_shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    depth = 0
    end = None
    for idx in range(start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        raise AssertionError(f"Could not find end of shell function {name}")
    return source[start:end]


class InstallModelScriptTests(unittest.TestCase):
    def test_moss_source_honors_exact_revision_and_rejects_missing_ref(self):
        bash_path = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(bash_path) if bash_path.exists() else shutil.which("bash")
        if not bash:
            self.skipTest("bash is not available")
        function = _extract_shell_function(INSTALLER.read_text(encoding="utf-8"), "ensure_moss_source")
        script = r'''
set -euo pipefail
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git init -q "$tmp/upstream"
mkdir -p "$tmp/upstream/moss_soundeffect_v2"
touch "$tmp/upstream/pyproject.toml" "$tmp/upstream/moss_soundeffect_v2/__init__.py"
git -C "$tmp/upstream" add .
git -C "$tmp/upstream" -c user.name=Test -c user.email=test@example.invalid -c commit.gpgsign=false commit -qm fixture
wanted="$(git -C "$tmp/upstream" rev-parse HEAD)"
OVERRIDES_DIR="$tmp/overrides"
mkdir -p "$OVERRIDES_DIR"
OMNI_MOSS_TTS_REPO="$tmp/upstream"
OMNI_MOSS_TTS_REF=missing-ref
''' + function + r'''
if ensure_moss_source; then echo 'Unexpected fallback to HEAD'; exit 9; fi
OMNI_MOSS_TTS_REF="$wanted"
ensure_moss_source
test "$(git -C "$OVERRIDES_DIR/moss_tts_repo" rev-parse HEAD)" = "$wanted"
OMNI_MOSS_TTS_REF=missing-ref
if ensure_moss_source; then echo 'Unexpected update fallback'; exit 10; fi
test "$(git -C "$OVERRIDES_DIR/moss_tts_repo" rev-parse HEAD)" = "$wanted"
'''
        result = subprocess.run([bash, "-s"], input=script.encode(), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_runtime_only_dispatch_never_enters_weight_installers(self):
        bash_path = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(bash_path) if bash_path.exists() else shutil.which("bash")
        if not bash:
            self.skipTest("bash is not available")
        source = INSTALLER.read_text(encoding="utf-8")
        branch = source.split('    runtimes)\n', 1)[1].split(
            '    install-missing-defaults|defaults)', 1)[0]
        allowed = (
            "install_qwen_override", "install_minicpm_override",
            "install_override_with_deps", "install_override",
            "install_override_unconstrained", "ensure_moss_source",
            "install_moss_tts_runtime", "install_moss_sfx_runtime",
            "install_anygpt_override",
        )
        stubs = "\n".join(f'{name}() {{ echo {name}; }}' for name in allowed)
        script = 'set -euo pipefail\n' + stubs + '\ncase "$1" in\nruntimes)\n' + branch + '\nesac\n'
        for family in ("qwen", "minicpm", "qwen3", "nemotron", "moshi", "anygpt",
                       "minimax_music3", "moss_tts", "moss_sfx"):
            with self.subTest(family=family):
                result = subprocess.run([bash, "-s", "--", "runtimes", family],
                                        input=script.encode(), capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertTrue(result.stdout.strip())
        rejected = subprocess.run([bash, "-s", "--", "runtimes", "all"],
                                  input=script.encode(), capture_output=True, timeout=10)
        self.assertNotEqual(rejected.returncode, 0)

    def test_install_override_without_packages_writes_marker_without_pip(self):
        bash = str(Path("C:/Program Files/Git/bin/bash.exe")) if Path("C:/Program Files/Git/bin/bash.exe").exists() else (None if __import__("os").name == "nt" else shutil.which("bash"))
        if not bash:
            self.skipTest("bash is not available")

        source = INSTALLER.read_text(encoding="utf-8")
        install_override = _extract_shell_function(source, "publish_override") + "\n" + _extract_shell_function(source, "install_override")
        script = f"""
set -euo pipefail
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

OVERRIDES_DIR="$tmp/overrides"
PIP="$tmp/fake-pip"
PIP_CONSTRAINT_ARGS=()
mkdir -p "$OVERRIDES_DIR"

cat > "$PIP" <<'PIP'
#!/usr/bin/env bash
echo called >> "$PIP_CALLED_LOG"
exit 12
PIP
chmod +x "$PIP"
export PIP_CALLED_LOG="$tmp/pip.called"

{install_override}

install_override "nemotron"
test -f "$OVERRIDES_DIR/nemotron/.install_complete"
test ! -e "$PIP_CALLED_LOG"
"""
        script = script.replace("\r\n", "\n")
        result = subprocess.run(
            [bash, "-s"],
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)

    def test_nemotron_default_installer_uses_marker_override_slot(self):
        source = INSTALLER.read_text(encoding="utf-8")
        install_nemotron = _extract_shell_function(
            source, "install_nemotron_nano_omni")

        self.assertIn('install_override "nemotron"', install_nemotron)
        self.assertIn("download_weights", install_nemotron)

    def test_installer_exposes_missing_defaults_orchestrator(self):
        source = INSTALLER.read_text(encoding="utf-8")
        install_defaults = _extract_shell_function(source, "install_missing_defaults")

        self.assertIn("default_installed_flag", install_defaults)
        self.assertIn("install_qwen", install_defaults)
        self.assertIn("install_audio_model", install_defaults)
        self.assertIn("install_ace_model", install_defaults)
        self.assertIn("install-missing-defaults|defaults)", source)

    def test_comfy_blueprint_batch_policy_reaches_shell_scanner(self):
        source = INSTALLER.read_text(encoding="utf-8")
        comfy_blueprints = _extract_shell_function(source, "download_comfy_blueprint_assets")

        self.assertIn("BLUEPRINT_CATEGORY_FILTER", comfy_blueprints)
        self.assertIn("BLUEPRINT_TEMPLATE_FILTER", comfy_blueprints)
        self.assertIn("extract_hf_links", comfy_blueprints)
        self.assertIn("category_filter and category not in category_filter", comfy_blueprints)
        self.assertIn("download_comfy_blueprint_assets \"${2:-}\" \"${3:-500}\" \"${4:-1}\" \"${5:-}\" \"${6:-}\"", source)

    def test_comfy_asset_optional_name_does_not_use_nul_case_pattern(self):
        source = INSTALLER.read_text(encoding="utf-8")
        comfy_asset = _extract_shell_function(source, "download_comfy_asset")
        comfy_url = _extract_shell_function(source, "download_comfy_url")

        # Bash strings cannot contain NUL.  $'\\0' becomes empty inside a
        # case glob and makes that branch match every otherwise valid name.
        self.assertNotIn("*$'\\0'*", comfy_asset)
        self.assertNotIn("*$'\\0'*", comfy_url)
        self.assertIn('local local_name="$4" # may be empty -> default basename', comfy_asset)


if __name__ == "__main__":
    unittest.main()
