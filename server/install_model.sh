#!/usr/bin/env bash
# =============================================================================
# Omni Studio -- Per-Model Installer
# Usage: bash install_model.sh <model_id>
#
# Installs model-specific packages (into override directory)
# and downloads model weights via HuggingFace Hub.
# =============================================================================

set -euo pipefail

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
    echo "Usage: $0 <command>"
    echo ""
    echo "Model installs: qwen_omni_3b, qwen_omni_7b, minicpm_o, moshi, anygpt, all"
    echo "Variants:       variant <repo> <weights_dir> [declared_gb]"
    echo "LoRA:           lora <repo> <name>"
    echo "Audio Lab:      audio-model <variant_id> | audio-vae <variant_id>"
    echo "                clap-model <variant_id>"
    echo "                audio-custom <repo> [name] [kind=model|vae|clap]"
    echo "                audio-list"
    echo "MiniMax Music:  minimax-music3-model [official-diffusers]"
    echo "MOSS:           moss_tts | moss_sfx"
    echo "ComfyUI asset:  comfy-asset <category> <repo> [file] [name]"
    echo "                comfy-url <category> <hf-resolve-url> [name]"
    echo "Blueprints:     comfy-blueprints [filename.json] [limit] [missing_only]"
    echo "Custom node:    comfy-node <repo_url> [ref]"
    echo "Update Comfy:   comfyui-update [ref]"
    echo "Maintenance:    repair-venv | prune-caches | verify-models [model_id]"
    echo "Defaults:       install-missing-defaults"
    exit 1
fi

ENV_CONF="/opt/omni_studio/env.conf"
if [ ! -f "$ENV_CONF" ]; then
    echo "ERROR: $ENV_CONF not found. Run setup.sh first."
    exit 1
fi
# The gateway assigns each download a bounded CPU budget. Preserve those
# per-job values across env.conf loading; env.conf remains the fallback for
# direct CLI invocations.
_RUNTIME_CPU_WORKERS="${OMNI_CPU_WORKERS:-}"
_RUNTIME_DOWNLOAD_WORKERS="${OMNI_DOWNLOAD_WORKERS:-}"
source "$ENV_CONF"
if [ -n "$_RUNTIME_CPU_WORKERS" ]; then
    OMNI_CPU_WORKERS="$_RUNTIME_CPU_WORKERS"
fi
if [ -n "$_RUNTIME_DOWNLOAD_WORKERS" ]; then
    OMNI_DOWNLOAD_WORKERS="$_RUNTIME_DOWNLOAD_WORKERS"
fi
unset _RUNTIME_CPU_WORKERS _RUNTIME_DOWNLOAD_WORKERS

# Critical base paths derived from env.conf are used as rm -rf / install /
# mkdir bases below. A truncated or hand-edited env.conf that omits one of
# these would otherwise leave the var empty and turn "$BASE/..." into a
# root-relative destructive path. Fail closed if any is unset or empty.
# (set -u above already catches the unset case; this also rejects empty.)
for _req in VENV_DIR MODELS_DIR OVERRIDES_DIR COMFYUI_DIR; do
    if [ -z "${!_req:-}" ]; then
        echo "ERROR: required path '$_req' is unset or empty in $ENV_CONF" >&2
        echo "       Refusing to proceed (would risk root-relative paths). Re-run setup.sh." >&2
        exit 1
    fi
done
unset _req

# ComfyUI model files are deliberately isolated from the shared model/cache
# hierarchy. Xet may use the shared HF cache, but final files always live here.
COMFY_MODELS_DIR="$COMFYUI_DIR/models"
COMFY_HF_CACHE_DIR="$COMFYUI_DIR/.cache/huggingface"

CACHE_DIR="${CACHE_DIR:-/opt/omni_studio/cache}"
RUNTIME_DIR="${RUNTIME_DIR:-$CACHE_DIR/runtime}"
mkdir -p "$RUNTIME_DIR"

case "$MODEL" in
    variant|lora|audio-model|audio-vae|clap-model|audio-custom|ace-model|ace-lm|ace-vae|minimax-music3-model|comfy-asset|comfy-url)
        # Independent weight targets may download concurrently. The API's
        # active-key guard prevents duplicate submissions; this per-target
        # lock provides the same protection to direct CLI invocations.
        LOCK_ID="$(printf '%s\0' "$@" | sha256sum | cut -c1-20)"
        LOCK_FILE="$RUNTIME_DIR/download_${LOCK_ID}.lock"
        LOCK_LABEL="download target"
        ;;
    *)
        # Dependency installs, venv repairs, Comfy updates, custom nodes, and
        # blueprint batches mutate shared state and remain serialized.
        LOCK_FILE="$RUNTIME_DIR/install.lock"
        LOCK_LABEL="shared installer"
        ;;
esac
exec 9>"$LOCK_FILE"
OMNI_INSTALL_LOCK_TIMEOUT_S="${OMNI_INSTALL_LOCK_TIMEOUT_S:-86400}"
if ! flock -n 9; then
    echo "Another $LOCK_LABEL process is already running; waiting for its lock..."
    if ! flock -w "$OMNI_INSTALL_LOCK_TIMEOUT_S" 9; then
        echo "ERROR: Timed out waiting for the install lock after ${OMNI_INSTALL_LOCK_TIMEOUT_S}s"
        exit 1
    fi
    echo "Install lock acquired; continuing."
else
    echo "Install lock acquired."
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python3"
SERVER_DIR="${SERVER_DIR:-/opt/omni_studio/server}"
export SERVER_DIR
PY_CONSTRAINTS="${SERVER_DIR:-/opt/omni_studio/server}/python_constraints.txt"
PIP_CONSTRAINT_ARGS=()
if [ -f "$PY_CONSTRAINTS" ]; then
    PIP_CONSTRAINT_ARGS=(-c "$PY_CONSTRAINTS")
fi

# Verify critical paths exist
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: Python not found at $PYTHON. Run setup.sh first."
    exit 1
fi
if [ ! -d "$MODELS_DIR" ]; then
    echo "ERROR: MODELS_DIR not accessible: $MODELS_DIR"
    echo "Run setup.sh to create the persistent model directory."
    exit 1
fi
mkdir -p "$MODELS_DIR/omni" "$MODELS_DIR/hub" "$MODELS_DIR/xet" "$MODELS_DIR/torch" \
    "$MODELS_DIR/lora" "$MODELS_DIR/hf_datasets" \
    "$CACHE_DIR/pip" "$CACHE_DIR/xdg" "$CACHE_DIR/tmp" "$CACHE_DIR/pycache" \
    "$CACHE_DIR/torch_extensions" "$CACHE_DIR/triton" "$CACHE_DIR/cuda" \
    "$CACHE_DIR/numba" "$CACHE_DIR/matplotlib" "$COMFY_MODELS_DIR" \
    "$COMFY_HF_CACHE_DIR/hub" "$COMFY_HF_CACHE_DIR/xet"

CPU_TOTAL="$(nproc 2>/dev/null || echo 2)"
OMNI_CPU_WORKERS="${OMNI_CPU_WORKERS:-$(( (CPU_TOTAL + 1) / 2 ))}"
if [ "$OMNI_CPU_WORKERS" -lt 1 ]; then
    OMNI_CPU_WORKERS=1
fi
OMNI_DOWNLOAD_WORKERS="${OMNI_DOWNLOAD_WORKERS:-$(( CPU_TOTAL / 3 ))}"
if [ "$OMNI_DOWNLOAD_WORKERS" -lt 1 ]; then
    OMNI_DOWNLOAD_WORKERS=1
fi

export HF_HOME="$MODELS_DIR"
export HUGGINGFACE_HUB_CACHE="$MODELS_DIR/hub"
export HF_XET_CACHE="$MODELS_DIR/xet"
export TORCH_HOME="$MODELS_DIR/torch"
export TRANSFORMERS_CACHE="$MODELS_DIR/hub"
export PIP_CACHE_DIR="$CACHE_DIR/pip"
export PIP_PROGRESS_BAR=off
export XDG_CACHE_HOME="$CACHE_DIR/xdg"
export TMPDIR="$CACHE_DIR/tmp"
export PYTHONPYCACHEPREFIX="$CACHE_DIR/pycache"
export TORCH_EXTENSIONS_DIR="$CACHE_DIR/torch_extensions"
export TRITON_CACHE_DIR="$CACHE_DIR/triton"
export CUDA_CACHE_PATH="$CACHE_DIR/cuda"
export NUMBA_CACHE_DIR="$CACHE_DIR/numba"
export MPLCONFIGDIR="$CACHE_DIR/matplotlib"
export HF_DATASETS_CACHE="$MODELS_DIR/hf_datasets"
export IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"
export PYTHONUNBUFFERED=1
export MAX_JOBS="$OMNI_CPU_WORKERS"
export CMAKE_BUILD_PARALLEL_LEVEL="$OMNI_CPU_WORKERS"
export MAKEFLAGS="-j$OMNI_CPU_WORKERS"
export NINJAFLAGS="-j$OMNI_CPU_WORKERS"
export OMP_NUM_THREADS="$OMNI_CPU_WORKERS"
export OPENBLAS_NUM_THREADS="$OMNI_CPU_WORKERS"
export MKL_NUM_THREADS="$OMNI_CPU_WORKERS"
export NUMEXPR_NUM_THREADS="$OMNI_CPU_WORKERS"
export HF_XET_NUM_CONCURRENT_RANGE_GETS="$OMNI_DOWNLOAD_WORKERS"
export OMNI_CPU_WORKERS
export OMNI_DOWNLOAD_WORKERS
export OMNI_APP_INSTANCE="${APP_DIR:-/opt/omni_studio}"
export PYTHONPATH="$SERVER_DIR${PYTHONPATH:+:$PYTHONPATH}"

echo "============================================"
echo "  Installing model: $MODEL"
echo "============================================"

ensure_free_space() {
    local required_gb="$1"
    local label="$2"
    local avail_gb
    avail_gb=$(df -BG "$MODELS_DIR" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
    # If df output is unparseable (empty / non-numeric), don't silently skip
    # the check -- warn loudly on stderr so the missing guard is visible, then
    # continue (a df quirk shouldn't hard-block an install).
    case "$avail_gb" in
        ""|*[!0-9]*)
            echo "WARNING: could not determine free space in $MODELS_DIR" \
                 "(df output unparseable: '$avail_gb'); skipping the" \
                 "${required_gb}GB free-space check for $label" >&2
            return 0
            ;;
    esac
    if [ "$avail_gb" -lt "$required_gb" ]; then
        echo "ERROR: $label needs at least ${required_gb}GB free in $MODELS_DIR ($avail_gb GB available)"
        echo "Free up disk space before downloading models."
        return 1
    fi
}

download_required_gb() {
    local declared_gb="${1:-1}"
    "$PYTHON" - "$declared_gb" <<'PYEOF'
import math
import os
import sys

try:
    declared = float(sys.argv[1])
except (IndexError, ValueError):
    declared = 1.0
try:
    multiplier = float(os.environ.get("OMNI_DOWNLOAD_HEADROOM_MULTIPLIER", "1.35"))
except ValueError:
    multiplier = 1.35
try:
    reserve = float(os.environ.get("OMNI_DOWNLOAD_HEADROOM_GB", "4"))
except ValueError:
    reserve = 4.0
print(max(1, int(math.ceil(declared * multiplier + reserve))))
PYEOF
}

# ---------------------------------------------------------------------------
# Helper: install into override directory
# ---------------------------------------------------------------------------
publish_override() {
    local target="$1"
    local staged="$2"
    local backup="${target}.previous"
    # Preserve a prior interrupted promotion for explicit recovery.
    if [ -e "$backup" ]; then
        if [ ! -e "$target" ]; then mv "$backup" "$target"; fi
        if [ -e "$backup" ]; then
            echo "ERROR: preserved override backup requires review: $backup" >&2
            return 1
        fi
    fi
    touch "$staged/.install_complete"
    if [ -e "$target" ]; then mv "$target" "$backup" || return 1; fi
    if ! mv "$staged" "$target"; then
        if [ -e "$backup" ]; then mv "$backup" "$target"; fi
        return 1
    fi
    if [ -e "$backup" ]; then rm -rf "$backup"; fi
}

install_override() {
    # Install packages into an override directory WITHOUT resolving transitive
    # deps. The shared venv already has torch, numpy, transformers, etc.
    # Without --no-deps, pip would download its own copy of torch into the
    # override, shadowing the shared venv's CUDA-enabled version.
    local name="$1"
    shift
    local target="$OVERRIDES_DIR/$name"
    local target_tmp="$OVERRIDES_DIR/.${name}.tmp.$$"
    rm -rf "$target_tmp"
    mkdir -p "$target_tmp"
    echo "Installing override packages to $target..."
    if [ "$#" -gt 0 ]; then
        "$PIP" install --target="$target_tmp" --upgrade --force-reinstall --no-deps "${PIP_CONSTRAINT_ARGS[@]}" "$@"
    else
        echo "No override packages required for $name; creating marker only."
    fi
    publish_override "$target" "$target_tmp"
}

install_override_unconstrained() {
    # A pinned model-specific package may intentionally sit outside the shared
    # venv's version constraint while still reusing all shared dependencies.
    # Keep it isolated and --no-deps, but do not make pip resolve the package
    # against a constraint that exists specifically for the shared copy.
    local name="$1"
    shift
    local target="$OVERRIDES_DIR/$name"
    local target_tmp="$OVERRIDES_DIR/.${name}.tmp.$$"
    rm -rf "$target_tmp"
    mkdir -p "$target_tmp"
    echo "Installing isolated override package to $target..."
    "$PIP" install --target="$target_tmp" --upgrade --force-reinstall --no-deps "$@"
    publish_override "$target" "$target_tmp"
}

install_override_with_deps() {
    # For packages with small unique deps not in the shared venv.
    # Installs normally but cleans out any torch/numpy/transformers that
    # got pulled in to prevent shadowing the shared venv.
    local name="$1"
    shift
    local target="$OVERRIDES_DIR/$name"
    local target_tmp="$OVERRIDES_DIR/.${name}.tmp.$$"
    rm -rf "$target_tmp"
    mkdir -p "$target_tmp"
    echo "Installing override packages (with deps) to $target..."
    "$PIP" install --target="$target_tmp" --upgrade --force-reinstall "${PIP_CONSTRAINT_ARGS[@]}" "$@"
    # Remove heavy shared packages that should come from base venv
    for shadow in torch torch-*.dist-info numpy numpy-*.dist-info \
                  transformers transformers-*.dist-info \
                  huggingface_hub huggingface_hub-*.dist-info \
                  nvidia nvidia_* triton; do
        rm -rf "$target_tmp/$shadow" 2>/dev/null
    done
    publish_override "$target" "$target_tmp"
}

install_qwen_override() {
    # GPTQModel 4.2.5 supports Qwen2.5-Omni and Omni's Torch/Transformers
    # versions, but its source build imports setuptools.command.bdist_wheel.
    # The shared venv deliberately retains setuptools<70 for legacy model
    # installers, so provide modern build tools only in this atomic temp tree.
    local target="$OVERRIDES_DIR/qwen"
    local target_tmp="$OVERRIDES_DIR/.qwen.tmp.$$"
    rm -rf "$target_tmp"
    mkdir -p "$target_tmp"
    echo "Installing Qwen override packages to $target..."

    "$PIP" install --target="$target_tmp" --upgrade --force-reinstall \
        "${PIP_CONSTRAINT_ARGS[@]}" \
        "qwen-omni-utils[decord]" \
        "logbar>=0.2.1,<1" \
        "tokenicer>=0.0.6,<1" \
        "device-smi>=0.5.3,<1"

    # Remove shared-stack shadows before GPTQModel's build probes Torch.
    for shadow in torch torch-*.dist-info numpy numpy-*.dist-info \
                  transformers transformers-*.dist-info \
                  huggingface_hub huggingface_hub-*.dist-info \
                  nvidia nvidia_* triton; do
        rm -rf "$target_tmp/$shadow" 2>/dev/null
    done

    "$PIP" install --target="$target_tmp" --upgrade --force-reinstall --no-deps \
        "setuptools>=70.1,<84" "wheel>=0.46.2,<0.47"
    PYTHONPATH="$target_tmp${PYTHONPATH:+:$PYTHONPATH}" \
        "$PIP" install --target="$target_tmp" --upgrade --force-reinstall \
        --no-build-isolation --no-deps "${PIP_CONSTRAINT_ARGS[@]}" \
        "gptqmodel==4.2.5"

    # Build tools must not shadow the shared runtime after the wheel exists.
    rm -rf "$target_tmp"/setuptools "$target_tmp"/setuptools-*.dist-info \
           "$target_tmp"/wheel "$target_tmp"/wheel-*.dist-info 2>/dev/null
    publish_override "$target" "$target_tmp"
}

install_minicpm_override() {
    # MiniCPM's unique packages declare Torch/Torchaudio/NumPy as dependencies.
    # Installing them transitively into --target downloads a duplicate CUDA
    # stack and can leave an incompatible nvidia/ tree ahead of the shared
    # venv. Install the known small runtime set without transitive deps and
    # reuse Omni's pinned shared scientific/CUDA stack.
    local target="$OVERRIDES_DIR/minicpm"
    local target_tmp="$OVERRIDES_DIR/.minicpm.tmp.$$"
    rm -rf "$target_tmp"
    mkdir -p "$target_tmp"
    echo "Installing MiniCPM override packages to $target..."
    "$PIP" install --target="$target_tmp" --upgrade --force-reinstall --no-deps \
        "${PIP_CONSTRAINT_ARGS[@]}" \
        "decord" \
        "vector-quantize-pytorch" \
        "vocos" \
        "einops>=0.8" \
        "einx>=0.3" \
        "encodec==0.1.1" \
        "frozendict"
    publish_override "$target" "$target_tmp"
}

# ---------------------------------------------------------------------------
# Helper: download weights from HuggingFace
# ---------------------------------------------------------------------------
download_weights() {
    local repo="$1"
    local local_name="$2"
    local declared_gb="${3:-8}"
    local required_gb
    required_gb="$(download_required_gb "$declared_gb")"

    echo ""
    echo "Downloading model weights: $repo"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        echo "Using saved HuggingFace token"
    fi

    ensure_free_space "$required_gb" "$local_name"

    export HF_REPO="$repo"
    export HF_LOCAL="$local_name"
    export HF_MODELS_DIR="$MODELS_DIR"

    local _download_monitor_pid=""
    (
        while true; do
            sleep "${OMNI_DOWNLOAD_HEARTBEAT_S:-30}"
            if [ -d "$MODELS_DIR/omni/.$local_name.omni-partial" ]; then
                _size="$(du -sh "$MODELS_DIR/omni/.$local_name.omni-partial" 2>/dev/null | awk '{print $1}')"
                echo "[download] $local_name local size: ${_size:-unknown}"
            else
                echo "[download] $local_name waiting for local folder..."
            fi
        done
    ) &
    _download_monitor_pid="$!"

    set +e
    "$PYTHON" -c "
import os, gc, json, sys
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
local_name = os.environ['HF_LOCAL']
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
os.environ['HF_XET_CACHE'] = os.path.join(models_dir, 'xet')
os.environ['TORCH_HOME'] = os.path.join(models_dir, 'torch')
os.environ['TRANSFORMERS_CACHE'] = os.path.join(models_dir, 'hub')
from hf_download import snapshot_download_retry as snapshot_download
from snapshot_install import (
    commit_staged_directory, inspect_snapshot, staging_directory,
    write_install_complete,
)
token = os.environ.get('HF_TOKEN') or None
target_dir = os.path.join(models_dir, 'omni', local_name)
local_dir = str(staging_directory(target_dir))
max_workers = max(1, int(os.environ.get('OMNI_DOWNLOAD_WORKERS', '4')))
snapshot_download(
    repo_id=repo,
    cache_dir=os.path.join(models_dir, 'hub'),
    local_dir=local_dir,
    token=token,
    max_workers=max_workers,
)
gc.collect()

# Verify: if there is a safetensors index, check all shards exist
idx_file = os.path.join(local_dir, 'model.safetensors.index.json')
if os.path.exists(idx_file):
    with open(idx_file) as f:
        idx = json.load(f)
    expected = sorted(set(idx['weight_map'].values()))
    missing = [s for s in expected if not os.path.exists(os.path.join(local_dir, s))]
    if missing:
        print(f'ERROR: {len(missing)} weight shards missing after download:', file=sys.stderr)
        for m in missing:
            print(f'  {m}', file=sys.stderr)
        sys.exit(1)
    print(f'Verified: all {len(expected)} shards present')
integrity = inspect_snapshot(local_dir, require_weights=True)
if not integrity['valid']:
    print('ERROR: downloaded snapshot failed integrity checks:', file=sys.stderr)
    for blocker in integrity['blockers'][:100]:
        print(f'  {blocker}', file=sys.stderr)
    sys.exit(1)
write_install_complete(local_dir, repo)
commit_staged_directory(local_dir, target_dir)
print(f'Download complete: {local_name}')
"
    _download_status="$?"
    if [ -n "$_download_monitor_pid" ]; then
        kill "$_download_monitor_pid" 2>/dev/null || true
        wait "$_download_monitor_pid" 2>/dev/null || true
    fi
    set -e
    return "$_download_status"
}

# ---------------------------------------------------------------------------
# Helper: download LoRA adapter from HuggingFace into models/lora/<name>/
# ---------------------------------------------------------------------------
download_lora() {
    local repo="$1"
    local local_name="$2"

    echo ""
    echo "Downloading LoRA adapter: $repo"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        echo "Using saved HuggingFace token"
    fi

    mkdir -p "$MODELS_DIR/lora"
    ensure_free_space "$(download_required_gb 1)" "$local_name"

    export HF_REPO="$repo"
    export HF_LOCAL="$local_name"
    export HF_MODELS_DIR="$MODELS_DIR"

    "$PYTHON" -c "
import os, gc, json, sys
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
local_name = os.environ['HF_LOCAL']
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
os.environ['HF_XET_CACHE'] = os.path.join(models_dir, 'xet')
os.environ['TORCH_HOME'] = os.path.join(models_dir, 'torch')
os.environ['TRANSFORMERS_CACHE'] = os.path.join(models_dir, 'hub')
from hf_download import snapshot_download_retry as snapshot_download
from snapshot_install import (
    commit_staged_directory, inspect_snapshot, staging_directory,
    write_install_complete,
)
token = os.environ.get('HF_TOKEN') or None
target_dir = os.path.join(models_dir, 'lora', local_name)
local_dir = str(staging_directory(target_dir))
max_workers = max(1, int(os.environ.get('OMNI_DOWNLOAD_WORKERS', '4')))
snapshot_download(
    repo_id=repo,
    cache_dir=os.path.join(models_dir, 'hub'),
    local_dir=local_dir,
    token=token,
    max_workers=max_workers,
)
gc.collect()

# Verify the adapter file exists
adapter_files = ['adapter_model.safetensors', 'adapter_model.bin']
found = any(os.path.exists(os.path.join(local_dir, f)) for f in adapter_files)
if not found:
    print(f'WARNING: No adapter_model.safetensors/bin in {local_dir}', file=sys.stderr)
    print(f'This may not be a PEFT LoRA adapter.', file=sys.stderr)
    sys.exit(2)
integrity = inspect_snapshot(local_dir, require_weights=True)
if not integrity['valid']:
    print('ERROR: downloaded LoRA failed integrity checks:', file=sys.stderr)
    for blocker in integrity['blockers'][:100]:
        print(f'  {blocker}', file=sys.stderr)
    sys.exit(1)
write_install_complete(local_dir, repo)
commit_staged_directory(local_dir, target_dir)
print(f'LoRA download complete: {local_name}')
"
}

# ---------------------------------------------------------------------------
# Model installers
# ---------------------------------------------------------------------------

install_qwen() {
    local variant="$1"  # "3b" or "7b"
    echo "Installing Qwen2.5-Omni override packages..."

    # qwen-omni-utils supplies the official multimodal preprocessing helpers.
    # GPTQModel is the maintained Transformers backend required by the
    # registry's pre-quantized Qwen 7B GPTQ variant. Keep both in the Qwen
    # override so their dependencies cannot disturb the shared runtime; the
    # The dedicated helper builds GPTQModel without changing the shared
    # Setuptools/Torch/Transformers stack.
    install_qwen_override

    if [ "$variant" = "3b" ]; then
        download_weights "Qwen/Qwen2.5-Omni-3B" "qwen-omni-3b" 8
    else
        download_weights "Qwen/Qwen2.5-Omni-7B" "qwen-omni-7b" 14
    fi
}

install_minicpm() {
    echo "Installing MiniCPM-o override packages..."

    install_minicpm_override

    download_weights "openbmb/MiniCPM-o-2_6" "minicpm-o" 16
}

install_moshi() {
    echo "Installing Moshi override packages..."

    # moshi pulls in its own torch if we don't block it.
    # Install moshi+rustymimi without deps, then add unique small deps.
    install_override "moshi" \
        "moshi" "rustymimi" "sphn" "einops" "sounddevice"
    # sentencepiece and huggingface_hub are already in the shared venv

    download_weights "kyutai/moshiko-pytorch-bf16" "moshi" 14
}

install_anygpt() {
    echo "Installing AnyGPT override packages..."

    # encodec and speechtokenizer are small -- no-deps is safe
    install_override "anygpt" \
        "encodec" "speechtokenizer"

    # NOTE: The DAMO-NLP-SG/AnyGPT repo is NOT cloned. It contains multi-GB
    # LFS objects and stalls on clone. The model loader uses standard
    # transformers AutoModelForCausalLM which doesn't need the repo source.

    download_weights "fnlp/AnyGPT-chat" "anygpt" 14
}

ensure_moss_source() {
    local repo="${OMNI_MOSS_TTS_REPO:-https://github.com/OpenMOSS/MOSS-TTS.git}"
    local ref="${OMNI_MOSS_TTS_REF:-main}"
    local dest="$OVERRIDES_DIR/moss_tts_repo"
    if [ -d "$dest/.git" ]; then
        echo "Updating MOSS source at $dest..."
        git -C "$dest" fetch --depth 1 origin "$ref" || git -C "$dest" fetch --depth 1 origin
        git -C "$dest" checkout --force FETCH_HEAD 2>/dev/null || git -C "$dest" checkout --force "$ref"
    else
        echo "Cloning MOSS source to $dest..."
        rm -rf "$dest"
        git clone --depth 1 --branch "$ref" "$repo" "$dest" 2>/dev/null || {
            git clone --depth 1 "$repo" "$dest"
            git -C "$dest" checkout --force "$ref" 2>/dev/null || true
        }
    fi
    if [ ! -f "$dest/pyproject.toml" ] || [ ! -d "$dest/moss_soundeffect_v2" ]; then
        echo "ERROR: MOSS source checkout is incomplete: $dest" >&2
        return 1
    fi
}

moss_torch_index_url() {
    echo "${OMNI_MOSS_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
}

ensure_moss_python_min() {
    local python_bin="$1"
    local major="$2"
    local minor="$3"
    "$python_bin" - "$major" "$minor" <<'PYEOF'
import sys
want = (int(sys.argv[1]), int(sys.argv[2]))
if sys.version_info[:2] < want:
    raise SystemExit(
        f"Python {want[0]}.{want[1]}+ required, found "
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
PYEOF
}

install_moss_tts_runtime() {
    local src="$OVERRIDES_DIR/moss_tts_repo"
    local venv="$OVERRIDES_DIR/moss_tts_venv"
    local torch_index
    torch_index="$(moss_torch_index_url)"
    echo "Installing MOSS-TTS runtime into $venv..."
    if [ ! -x "$venv/bin/python3" ]; then
        python3 -m venv "$venv"
    fi
    ensure_moss_python_min "$venv/bin/python3" 3 10
    "$venv/bin/pip" install --upgrade pip setuptools wheel
    "$venv/bin/pip" install --index-url "$torch_index" \
        "torch==2.9.1+cu128" "torchaudio==2.9.1+cu128"
    "$venv/bin/pip" install \
        "safetensors==0.6.2" "numpy==2.1.0" "orjson==3.11.4" \
        "tqdm==4.67.1" "PyYAML==6.0.3" "einops==0.8.1" \
        "scipy==1.16.2" "librosa==0.11.0" "tiktoken==0.12.0" \
        psutil packaging ninja "transformers==5.0.0" "accelerate>=1.10.1" \
        "torchcodec==0.8.1" fastapi uvicorn python-multipart pydantic httpx soundfile
    "$venv/bin/pip" install --no-deps -e "$src"
    PYTHONPATH="$src/moss_tts_local_v1.5:$src" "$venv/bin/python3" - <<'PYEOF'
from streaming import StreamingRequest, load_runtime
from transformers import AutoModel, AutoProcessor
print("MOSS-TTS runtime imports OK")
PYEOF
    touch "$venv/.install_complete"
}

install_moss_sfx_runtime() {
    local src="$OVERRIDES_DIR/moss_tts_repo"
    local venv="$OVERRIDES_DIR/moss_sfx_venv"
    local torch_index
    torch_index="$(moss_torch_index_url)"
    echo "Installing MOSS-SoundEffect runtime into $venv..."
    if [ -x "$venv/bin/python3" ] && ! "$venv/bin/python3" - <<'PYEOF'
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)
PYEOF
    then
        if command -v python3.12 >/dev/null 2>&1; then
            echo "Existing MOSS-SoundEffect venv is below Python 3.12; recreating it."
            rm -rf "$venv"
        fi
    fi
    if command -v python3.12 >/dev/null 2>&1; then
        if [ ! -x "$venv/bin/python3" ]; then
            python3.12 -m venv "$venv"
        fi
    elif [ ! -x "$venv/bin/python3" ]; then
        python3 -m venv "$venv"
    fi
    ensure_moss_python_min "$venv/bin/python3" 3 12
    "$venv/bin/pip" install --upgrade pip setuptools wheel
    "$venv/bin/pip" install --index-url "$torch_index" \
        "torch==2.9.0+cu128" "torchaudio==2.9.0+cu128" \
        "torchvision==0.24.0+cu128" "torchcodec==0.8.0"
    "$venv/bin/pip" install \
        "numpy==1.26.4" "einops==0.8.2" "pillow==12.2.0" \
        "tqdm==4.67.3" "safetensors==0.7.0" "transformers==4.57.1" \
        "diffusers==0.37.1" "ftfy==6.3.1" "regex==2026.4.4" \
        "soundfile==0.13.1" "imageio==2.37.3" "typing-extensions>=4.10" \
        "descript-audiotools==0.7.2" fastapi uvicorn pydantic httpx
    "$venv/bin/pip" install --no-deps -e "$src/moss_soundeffect_v2"
    PYTHONPATH="$src" "$venv/bin/python3" - <<'PYEOF'
from moss_soundeffect_v2 import MossSoundEffectPipeline
print("MOSS-SoundEffect runtime imports OK")
PYEOF
    touch "$venv/.install_complete"
}

install_moss_tts() {
    ensure_moss_source
    install_moss_tts_runtime
    download_weights "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5" "moss-tts-local-v1.5" 10
    download_weights "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2" "moss-audio-tokenizer-v2" 2
}

install_moss_sfx() {
    ensure_moss_source
    install_moss_sfx_runtime
    download_weights "OpenMOSS-Team/MOSS-SoundEffect-v2.0" "moss-soundeffect-v2.0" 8
}

install_qwen3_omni() {
    echo "Installing Qwen3-Omni override packages..."

    # Reuse the qwen-omni-utils stack (decord/av video helpers). Qwen3-Omni
    # ships its own modeling code via transformers >=4.57 so no extra
    # transformers patch is needed.
    install_override_with_deps "qwen3" \
        "qwen-omni-utils[decord]"

    # Default variant for the Install Base button is the Instruct checkpoint.
    # Other variants (Thinking, Captioner) are installed via the Setup-tab
    # variant dropdown -> Download flow which calls download_weights directly.
    download_weights "Qwen/Qwen3-Omni-30B-A3B-Instruct" "qwen3-omni-30b-instruct" 60
}

install_nemotron_nano_omni() {
    echo "Installing Nemotron 3 Nano Omni override packages..."

    # NVIDIA Nemotron Nano Omni relies on trust_remote_code modeling files.
    # No extra Python deps are required for inference -- the override slot
    # is created so future helper packages (e.g. NVIDIA's NeMo runtime) can
    # be added without a setup-time migration.
    install_override "nemotron"

    # The BF16 reasoning checkpoint is the canonical default; FP8 / NVFP4
    # variants are reachable from the Setup-tab variant dropdown.
    download_weights "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16" \
        "nemotron-nano-omni-30b-bf16" 62
}

# ---------------------------------------------------------------------------
# Helper: download a single ComfyUI asset (checkpoint / VAE / CLIP / etc.)
# ---------------------------------------------------------------------------
download_comfy_asset() {
    local category="$1"
    local repo="$2"
    local hub_file="$3"   # may be empty -> snapshot the whole repo
    local local_name="$4" # may be empty -> default basename

    local valid_categories=" checkpoints diffusion_models vae clip text_encoders loras controlnet gguf unet embeddings upscale_models latent_upscale_models clip_vision model_patches style_models audio_encoders clip_projections diffusers configs gligen hypernetworks vae_approx frame_interpolation photomaker background_removal detection geometry_estimation optical_flow "
    case "$valid_categories" in
        *" $category "*) ;;
        *) echo "ERROR: invalid ComfyUI category: $category"; return 1 ;;
    esac
    # Defense in depth: even after the allowlist match, refuse separators
    # so a future category list with slashes can never enable traversal.
    case "$category" in
        */*|*\\*|*..*) echo "ERROR: invalid category characters: $category"; return 1 ;;
    esac
    if [ -z "$repo" ]; then
        echo "ERROR: comfy-asset requires <category> <repo> [file] [name]"
        return 1
    fi
    # SHE-4: local_name (-> HF_LOCAL) becomes the on-disk filename under
    # target_dir; apply the same traversal validation used for category so a
    # crafted name cannot escape the comfyui category directory. Reject path
    # separators, '..', a leading '/', and backslashes. POSIX argv cannot
    # contain NUL; spelling NUL as $'\0' in a shell case pattern collapses to
    # an empty string and therefore matches every filename, including empty.
    case "$local_name" in
        /*|*\\*|*//*|.|..|./*|../*|*/./*|*/../*|*/.|*/..) echo "ERROR: invalid comfy-asset name: $local_name"; return 1 ;;
    esac
    local target_dir="$COMFY_MODELS_DIR/$category"
    mkdir -p "$target_dir"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
    fi

    ensure_free_space 5 "$category/$(basename "$repo")"

    export HF_REPO="$repo"
    export HF_FILE="$hub_file"
    export HF_LOCAL="$local_name"
    export HF_TARGET_DIR="$target_dir"
    export HF_MODELS_DIR="$COMFY_HF_CACHE_DIR"

    "$PYTHON" -c "
import os, shutil, sys
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
target = os.environ['HF_TARGET_DIR']
hub_file = os.environ.get('HF_FILE') or ''
local_name = os.environ.get('HF_LOCAL') or ''
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
os.environ['HF_XET_CACHE'] = os.path.join(models_dir, 'xet')
token = os.environ.get('HF_TOKEN') or None
max_workers = max(1, int(os.environ.get('OMNI_DOWNLOAD_WORKERS', '4')))

import importlib.util
if importlib.util.find_spec('hf_xet') is None:
    raise RuntimeError('hf_xet is not installed; run setup or repair-venv before ComfyUI model downloads')

if hub_file:
    from hf_download import hf_hub_download_retry as hf_hub_download
    out_path = hf_hub_download(
        repo_id=repo,
        filename=hub_file,
        cache_dir=os.path.join(models_dir, 'hub'),
        token=token,
    )
    name = local_name or os.path.basename(hub_file)
    dest = os.path.join(target, name)
    target_real = os.path.realpath(target)
    dest_real = os.path.realpath(dest)
    if os.path.commonpath([target_real, dest_real]) != target_real:
        raise RuntimeError(f'invalid destination outside ComfyUI category: {name}')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.abspath(out_path) != os.path.abspath(dest) or os.path.islink(dest):
        from file_materialize import materialize_cached_file
        materialize_cached_file(out_path, dest)
    print(f'Installed {dest}')
else:
    from huggingface_hub import HfApi
    from hf_download import snapshot_download_retry as snapshot_download
    namespace = local_name or repo.replace('/', '_')
    isolated = os.path.realpath(os.path.join(target, namespace))
    if isolated == os.path.realpath(target) or os.path.commonpath([os.path.realpath(target), isolated]) != os.path.realpath(target):
        raise RuntimeError('Repository snapshot destination must be inside its category')
    metadata = HfApi(token=token).model_info(repo, files_metadata=True)
    files = metadata.siblings or []
    if not files or any(item.size is None for item in files):
        raise RuntimeError('Cannot establish repository size; select an explicit file instead')
    required = sum(item.size for item in files) * 2 + 2 * 1024**3
    if shutil.disk_usage(target).free < required:
        raise RuntimeError(f'Insufficient space for repository snapshot: requires {required} bytes')
    target = isolated
    os.makedirs(target, exist_ok=True)
    snapshot_download(
        repo_id=repo,
        cache_dir=os.path.join(models_dir, 'hub'),
        local_dir=target,
        token=token,
        max_workers=max_workers,
    )
    # local_dir behavior differs between huggingface_hub releases. Materialize
    # any cache symlinks so deleting a Comfy model always deletes its own file.
    from pathlib import Path
    for item in Path(target).rglob('*'):
        if not item.is_symlink():
            continue
        source = item.resolve(strict=True)
        from file_materialize import materialize_cached_file
        materialize_cached_file(source, item)
    print(f'Snapshot of {repo} into {target}')
"
}


# ---------------------------------------------------------------------------
# Helper: download a single ComfyUI asset from a HuggingFace/Xet URL
# ---------------------------------------------------------------------------
download_comfy_url() {
    local category="$1"
    local url="$2"
    local local_name="$3" # may be empty -> default basename

    local valid_categories=" checkpoints diffusion_models vae clip text_encoders loras controlnet gguf unet embeddings upscale_models latent_upscale_models clip_vision model_patches style_models audio_encoders clip_projections diffusers configs gligen hypernetworks vae_approx frame_interpolation photomaker background_removal detection geometry_estimation optical_flow "
    case "$valid_categories" in
        *" $category "*) ;;
        *) echo "ERROR: invalid ComfyUI category: $category"; return 1 ;;
    esac
    case "$category" in
        */*|*\\*|*..*) echo "ERROR: invalid category characters: $category"; return 1 ;;
    esac
    if [ -z "$url" ]; then
        echo "ERROR: comfy-url requires <category> <hf-resolve-url> [name]"
        return 1
    fi
    case "$local_name" in
        /*|*\\*|*//*|.|..|./*|../*|*/./*|*/../*|*/.|*/..) echo "ERROR: invalid comfy-url name: $local_name"; return 1 ;;
    esac
    local target_dir="$COMFY_MODELS_DIR/$category"
    mkdir -p "$target_dir"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
    fi

    ensure_free_space 5 "$category/direct-url"

    export HF_URL="$url"
    export HF_LOCAL="$local_name"
    export HF_TARGET_DIR="$target_dir"
    export HF_MODELS_DIR="$COMFY_HF_CACHE_DIR"

    "$PYTHON" <<'PY'
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

models_dir = os.environ["HF_MODELS_DIR"]
url = os.environ["HF_URL"].strip()
target = Path(os.environ["HF_TARGET_DIR"])
local_name = os.environ.get("HF_LOCAL") or ""
token = os.environ.get("HF_TOKEN") or None
hub_cache = os.path.join(models_dir, "hub")
os.environ["HF_HOME"] = models_dir
os.environ["HUGGINGFACE_HUB_CACHE"] = hub_cache
os.environ["HF_XET_CACHE"] = os.path.join(models_dir, "xet")

if importlib.util.find_spec("hf_xet") is None:
    print("ERROR: hf_xet is not installed; run setup or repair-venv before ComfyUI model downloads", file=sys.stderr)
    sys.exit(1)

def parse_hf_url(raw):
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.netloc.lower() != "huggingface.co":
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    marker = None
    for candidate in ("resolve", "blob"):
        if candidate in parts:
            marker = parts.index(candidate)
            break
    if marker is None or marker < 1 or len(parts) <= marker + 2:
        return None
    repo = "/".join(parts[:marker])
    revision = parts[marker + 1]
    hub_file = "/".join(parts[marker + 2:])
    if not repo or not revision or not hub_file:
        return None
    return repo, revision, hub_file

parsed = parse_hf_url(url)
if not parsed:
    print("ERROR: comfy-url only accepts HuggingFace /resolve/ or /blob/ URLs so downloads use HuggingFace Hub + hf_xet", file=sys.stderr)
    sys.exit(1)

repo, revision, hub_file = parsed
name = local_name or Path(hub_file).name
parts = name.split("/")
if (not name or name.startswith("/") or "\\" in name
        or any(part in ("", ".", "..") for part in parts)):
    print(f"ERROR: invalid destination filename: {name}", file=sys.stderr)
    sys.exit(1)

from hf_download import hf_hub_download_retry as hf_hub_download
src = Path(hf_hub_download(
    repo_id=repo,
    filename=hub_file,
    revision=revision,
    cache_dir=hub_cache,
    token=token,
))
dest = target / name
target_real = target.resolve()
dest_real = dest.resolve(strict=False)
try:
    dest_real.relative_to(target_real)
except ValueError:
    print(f"ERROR: destination escapes ComfyUI category: {name}", file=sys.stderr)
    sys.exit(1)
dest.parent.mkdir(parents=True, exist_ok=True)
if src.resolve() != dest.resolve() or dest.is_symlink():
    from file_materialize import materialize_cached_file
    materialize_cached_file(src, dest)
print(f"Installed {dest} from {repo}/{hub_file} via HuggingFace Hub + hf_xet")
PY
}


# ---------------------------------------------------------------------------
# Helper: download assets declared by the live ComfyUI blueprint templates
# ---------------------------------------------------------------------------
download_comfy_blueprint_assets() {
    local blueprint_file="${1:-}"
    local limit="${2:-500}"
    local missing_only="${3:-1}"
    local category_filter="${4:-}"
    local template_filter="${5:-}"

    case "$blueprint_file" in
        */*|*\\*|*..*) echo "ERROR: invalid blueprint filename: $blueprint_file"; return 1 ;;
    esac
    case "$limit" in
        ""|*[!0-9]*) echo "ERROR: blueprint asset limit must be a positive integer"; return 1 ;;
    esac
    case "$missing_only" in
        0|1|true|false|True|False) ;;
        *) echo "ERROR: missing_only must be 0/1 or true/false"; return 1 ;;
    esac
    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
    fi

    ensure_free_space 5 "ComfyUI blueprint assets"

    export BLUEPRINT_FILE="$blueprint_file"
    export BLUEPRINT_LIMIT="$limit"
    export BLUEPRINT_MISSING_ONLY="$missing_only"
    export BLUEPRINT_CATEGORY_FILTER="$category_filter"
    export BLUEPRINT_TEMPLATE_FILTER="$template_filter"
    export BLUEPRINT_DIR="$COMFYUI_DIR/blueprints"
    export OMNI_SAVED_WORKFLOWS_DIR="${WORKFLOWS_DIR:-/opt/omni_studio/workflows}"
    export COMFYUI_ROOT="$COMFYUI_DIR"
    export COMFY_MODELS_ROOT="$COMFY_MODELS_DIR"
    export HF_MODELS_DIR="$COMFY_HF_CACHE_DIR"
    export COMFY_CATEGORIES="checkpoints diffusion_models vae clip text_encoders loras controlnet gguf unet embeddings upscale_models latent_upscale_models clip_vision model_patches style_models audio_encoders clip_projections diffusers configs gligen hypernetworks vae_approx frame_interpolation photomaker background_removal detection geometry_estimation optical_flow"

    "$PYTHON" <<'PY'
import json
import importlib.metadata
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

models_dir = Path(os.environ["HF_MODELS_DIR"])
comfy_models_root = Path(os.environ["COMFY_MODELS_ROOT"])
blueprint_dir = Path(os.environ["BLUEPRINT_DIR"])
comfyui_root = Path(os.environ["COMFYUI_ROOT"])
filename = os.environ.get("BLUEPRINT_FILE", "").strip()
limit = int(os.environ.get("BLUEPRINT_LIMIT", "500") or "500")
missing_raw = os.environ.get("BLUEPRINT_MISSING_ONLY", "1").strip().lower()
missing_only = missing_raw not in ("0", "false")
categories = set(os.environ.get("COMFY_CATEGORIES", "").split())
category_filter = {c for c in os.environ.get("BLUEPRINT_CATEGORY_FILTER", "").split(",") if c}
template_filter = {t.strip() for t in os.environ.get("BLUEPRINT_TEMPLATE_FILTER", "").split(",") if t.strip()}
token = os.environ.get("HF_TOKEN") or None
hub_cache = models_dir / "hub"
os.environ["HF_HOME"] = str(models_dir)
os.environ["HUGGINGFACE_HUB_CACHE"] = str(hub_cache)
os.environ["HF_XET_CACHE"] = str(models_dir / "xet")

import importlib.util
if importlib.util.find_spec("hf_xet") is None:
    print("ERROR: hf_xet is not installed; run setup or repair-venv before ComfyUI blueprint downloads", file=sys.stderr)
    sys.exit(1)

if limit < 1:
    print("ERROR: blueprint asset limit must be at least 1", file=sys.stderr)
    sys.exit(1)
unknown_categories = sorted(c for c in category_filter if c not in categories)
if unknown_categories:
    print(f"ERROR: unknown ComfyUI categories in batch policy: {unknown_categories}", file=sys.stderr)
    sys.exit(1)

def blueprint_files():
    files = []
    if blueprint_dir.exists():
        files.extend(blueprint_dir.rglob("*.json"))
    saved = Path(os.environ["OMNI_SAVED_WORKFLOWS_DIR"])
    if saved.exists():
        files.extend(saved.rglob("*.json"))
    user_workflows = comfyui_root / "user" / "default" / "workflows"
    if user_workflows.exists():
        files.extend(user_workflows.rglob("*.json"))
    try:
        distributions = list(importlib.metadata.distributions())
    except Exception as exc:
        print(f"WARNING: could not enumerate ComfyUI template packages: {exc}", file=sys.stderr)
        distributions = []
    for dist in distributions:
        raw_name = str(dist.metadata.get("Name") or "")
        normalized = re.sub(r"[-_.]+", "-", raw_name).lower()
        if not (
            normalized.startswith("comfyui-workflow-templates")
            or normalized.startswith("comfyui-subgraph-blueprints")
        ):
            continue
        for relative in dist.files or ():
            relative_path = Path(str(relative))
            parts = tuple(part.lower() for part in relative_path.parts)
            if relative_path.suffix.lower() != ".json":
                continue
            if "templates" not in parts and "blueprints" not in parts:
                continue
            try:
                path = Path(dist.locate_file(relative)).resolve()
            except (OSError, TypeError, ValueError):
                continue
            if path.is_file():
                files.append(path)
    files = sorted(set(files))
    if filename:
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            print(f"ERROR: invalid template selector: {filename}", file=sys.stderr)
            sys.exit(1)
        selector_stem = Path(filename).stem
        matches = [path for path in files if path.name == filename or path.stem == selector_stem]
        if not matches:
            print(f"ERROR: template not found: {filename}", file=sys.stderr)
            sys.exit(1)
        return matches
    if template_filter:
        files = [
            path for path in files
            if path.name in template_filter or path.stem in template_filter
        ]
    return files

def extract_models(value):
    found = []
    if isinstance(value, dict):
        models = value.get("models")
        if isinstance(models, list):
            found.extend(item for item in models if isinstance(item, dict))
        for child in value.values():
            found.extend(extract_models(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(extract_models(child))
    return found

def clean_url(raw):
    return raw.rstrip(").,;]")

def parse_hf_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "huggingface.co":
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    marker = None
    for candidate in ("resolve", "blob"):
        if candidate in parts:
            marker = parts.index(candidate)
            break
    if marker is None or marker < 1 or len(parts) <= marker + 2:
        return None
    repo = "/".join(parts[:marker])
    revision = parts[marker + 1]
    hub_file = "/".join(parts[marker + 2:])
    if not repo or not revision or not hub_file:
        return None
    return repo, revision, hub_file

def extract_hf_links(value):
    found = []
    if isinstance(value, str):
        for raw in re.findall(r"https?://[^\s\"'<>]+", value):
            url = clean_url(raw)
            if parse_hf_url(url):
                found.append(url)
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(extract_hf_links(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(extract_hf_links(child))
    return found

def installed(category, name):
    target = comfy_models_root / category / name
    return target.exists() and target.is_file() and target.stat().st_size > 0

def normalise(raw, blueprint):
    category = str(raw.get("directory") or raw.get("save_path") or "").strip()
    if category not in categories:
        return None
    if category_filter and category not in category_filter:
        return None
    name = str(raw.get("filename") or raw.get("name") or "").strip().replace("\\", "/").strip("/")
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        return None
    url = str(raw.get("url") or "").strip()
    if not name or not url:
        return None
    if not parse_hf_url(url):
        return None
    return {
        "blueprint": blueprint,
        "category": category,
        "name": name,
        "url": url,
        "installed": installed(category, name),
    }

def category_from_hub_file(hub_file):
    parts = [part for part in hub_file.split("/") if part]
    for part in reversed(parts[:-1]):
        if part in categories:
            return part
    return None

def normalise_link(url, blueprint):
    parsed = parse_hf_url(url)
    if not parsed:
        return None
    _repo, _revision, hub_file = parsed
    category = category_from_hub_file(hub_file)
    if category not in categories:
        return None
    if category_filter and category not in category_filter:
        return None
    name = Path(hub_file).name
    if Path(name).suffix.lower() not in {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf"}:
        return None
    return {
        "blueprint": blueprint,
        "category": category,
        "name": name,
        "url": url,
        "installed": installed(category, name),
    }

items = []
seen = set()
for path in blueprint_files():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: skipping unreadable blueprint {path.name}: {exc}", file=sys.stderr)
        continue
    for raw in extract_models(data):
        model = normalise(raw, path.name)
        if model is None:
            continue
        key = (model["category"], model["name"])
        if key in seen:
            continue
        seen.add(key)
        items.append(model)
    for url in extract_hf_links(data):
        model = normalise_link(url, path.name)
        if model is None:
            continue
        key = (model["category"], model["name"])
        if key in seen:
            continue
        seen.add(key)
        items.append(model)

missing = [item for item in items if not item["installed"]]
pending = missing if missing_only else items
pending = pending[:limit]
policy = []
if category_filter:
    policy.append("categories=" + ",".join(sorted(category_filter)))
if template_filter:
    policy.append("templates=" + ",".join(sorted(template_filter)))
print(
    f"Blueprint assets: {len(missing)} missing of {len(items)} declared; installing {len(pending)}"
    + (f" ({'; '.join(policy)})" if policy else "")
)
if not pending:
    sys.exit(0)

def place_file(src, dest):
    from file_materialize import materialize_cached_file
    materialize_cached_file(src, dest)

failures = []
for index, item in enumerate(pending, start=1):
    category = item["category"]
    name = item["name"]
    dest = comfy_models_root / category / name
    if missing_only and installed(category, name):
        print(f"Skipping existing: {category}/{name}")
        continue
    print(f"Installing blueprint asset {index}/{len(pending)}: {category}/{name}")
    try:
        hf = parse_hf_url(item["url"])
        if hf:
            from hf_download import hf_hub_download_retry as hf_hub_download
            repo, revision, hub_file = hf
            src = hf_hub_download(
                repo_id=repo,
                filename=hub_file,
                revision=revision,
                cache_dir=str(hub_cache),
                token=token,
            )
            place_file(Path(src), dest)
        else:
            raise RuntimeError("unsupported non-HuggingFace URL; ComfyUI model downloads must use HuggingFace Hub + hf_xet")
        print(f"Installed {dest}")
    except Exception as exc:
        failures.append(f"{category}/{name}: {exc}")
        print(f"ERROR: failed {category}/{name}: {exc}", file=sys.stderr)

if failures:
    print("Blueprint asset install failures:", file=sys.stderr)
    for failure in failures:
        print(f" - {failure}", file=sys.stderr)
    sys.exit(1)
PY
}


# ---------------------------------------------------------------------------
# Helper: clone a ComfyUI custom node
# ---------------------------------------------------------------------------
install_comfy_node() {
    local repo_url="$1"
    local ref="${2:-}"
    if [ -z "$repo_url" ]; then
        echo "ERROR: comfy-node requires <repo_url> [ref]"
        return 1
    fi
    # Conservative URL pattern: http(s) or git@host:org/repo.git.
    case "$repo_url" in
        http://*|https://*|git@*:*) ;;
        *) echo "ERROR: comfy-node refuses non-http(s)/ssh URL: $repo_url"; return 1 ;;
    esac
    local name="$(basename "$repo_url" .git)"
    case "$name" in
        ""|.|..|*/*|*\\*) echo "ERROR: bad derived node name from URL: $name"; return 1 ;;
    esac
    local target="$COMFYUI_DIR/custom_nodes/$name"
    if [ -d "$target/.git" ]; then
        local actual_origin
        actual_origin="$(git -C "$target" remote get-url origin)" || return 1
        local requested_identity="${repo_url%.git}"
        local actual_identity="${actual_origin%.git}"
        requested_identity="${requested_identity#https://}"
        requested_identity="${requested_identity#http://}"
        requested_identity="${requested_identity#git@}"
        actual_identity="${actual_identity#https://}"
        actual_identity="${actual_identity#http://}"
        actual_identity="${actual_identity#git@}"
        if [ "${requested_identity/:/\/}" != "${actual_identity/:/\/}" ]; then
            echo "ERROR: custom-node directory belongs to another repository: $name" >&2
            return 1
        fi
        echo "Updating existing custom node: $name"
        if [ -n "$ref" ]; then
            if ! git -C "$target" fetch --depth 1 origin "$ref"; then
                echo "ERROR: failed to fetch ref '$ref' from $repo_url"
                return 1
            fi
            if ! git -C "$target" checkout --detach --force FETCH_HEAD; then
                echo "ERROR: failed to check out ref '$ref'"
                return 1
            fi
        else
            git -C "$target" fetch --depth 1 origin
            git -C "$target" checkout --force FETCH_HEAD
        fi
    else
        echo "Cloning custom node $repo_url -> $target"
        if ! git clone --filter=blob:none --no-checkout "$repo_url" "$target"; then
            echo "ERROR: git clone failed for $repo_url"
            return 1
        fi
        if [ -n "$ref" ]; then
            if ! git -C "$target" fetch --depth 1 origin "$ref"; then
                echo "ERROR: failed to fetch ref '$ref'"
                rm -rf "$target"
                return 1
            fi
            if ! git -C "$target" checkout --detach --force FETCH_HEAD; then
                echo "ERROR: failed to check out ref '$ref'"
                rm -rf "$target"
                return 1
            fi
        else
            git -C "$target" checkout --force HEAD || git -C "$target" checkout --force FETCH_HEAD
        fi
    fi
    if [ -f "$target/requirements.txt" ]; then
        echo "Installing requirements for $name..."
        "$PIP" install -q -r "$target/requirements.txt" "${PIP_CONSTRAINT_ARGS[@]}"
    fi
    echo "Custom node ready: $name"
}


# ---------------------------------------------------------------------------
# Helper: refresh the pinned ComfyUI checkout
# ---------------------------------------------------------------------------
update_comfyui() {
    local ref="${1:-${OMNI_COMFYUI_REF:-}}"
    if [ -z "$ref" ]; then
        echo "ERROR: comfyui-update requires <ref> or OMNI_COMFYUI_REF env var"
        return 1
    fi
    if [ ! -d "$COMFYUI_DIR/.git" ]; then
        echo "ERROR: ComfyUI not installed at $COMFYUI_DIR -- run setup.sh first"
        return 1
    fi
    # Omni replaces Comfy's tracked placeholder files below input/models/user
    # with persistent runtime directories. Ignore only those expected layout
    # deletions; any other tracked source change still blocks a force checkout.
    local tracked_changes source_changes
    tracked_changes="$(git -C "$COMFYUI_DIR" status --porcelain --untracked-files=no)"
    source_changes="$(printf '%s\n' "$tracked_changes" | awk '
        NF {
            line=$0
            path=substr(line, 4)
            if (substr(line, 2, 1) == " ") path=substr(line, 3)
            gsub(/^"|"$/, "", path)
            split(path, parts, "/")
            if (parts[1] != "input" && parts[1] != "models" && parts[1] != "user") print line
        }
    ')"
    if [ -n "$source_changes" ]; then
        echo "ERROR: ComfyUI checkout has tracked changes; refusing to overwrite them"
        return 1
    fi
    local previous_commit fetch_ref target_commit
    previous_commit="$(git -C "$COMFYUI_DIR" rev-parse HEAD)"
    fetch_ref="$ref"
    if [ "$ref" = "latest" ]; then
        fetch_ref="HEAD"
    fi
    echo "Updating ComfyUI from $previous_commit to $ref..."
    if ! git -C "$COMFYUI_DIR" fetch --depth 1 origin "$fetch_ref"; then
        echo "ERROR: failed to fetch ref '$ref' from origin"
        return 1
    fi
    target_commit="$(git -C "$COMFYUI_DIR" rev-parse FETCH_HEAD)"
    if ! git -C "$COMFYUI_DIR" checkout --detach --force "$target_commit"; then
        echo "ERROR: failed to check out ref '$ref'"
        git -C "$COMFYUI_DIR" checkout --detach --force "$previous_commit" || true
        return 1
    fi
    echo "Re-installing ComfyUI requirements..."
    local _constraints
    _constraints="$(mktemp)"
    echo "torch>=2.6" > "$_constraints"
    if ! "$PIP" install -q -r "$COMFYUI_DIR/requirements.txt" -c "$_constraints" "${PIP_CONSTRAINT_ARGS[@]}"; then
        echo "ERROR: requirements install failed; rolling back to $previous_commit"
        git -C "$COMFYUI_DIR" checkout --detach --force "$previous_commit" || true
        "$PIP" install -q -r "$COMFYUI_DIR/requirements.txt" -c "$_constraints" "${PIP_CONSTRAINT_ARGS[@]}" || true
        rm -f "$_constraints"
        return 1
    fi
    rm -f "$_constraints"
    echo "ComfyUI updated to $target_commit (requested $ref; previous $previous_commit)"
}


# ---------------------------------------------------------------------------
# Helper: rerun the venv block from setup.sh in repair mode
# ---------------------------------------------------------------------------
repair_venv() {
    local setup_sh="${SERVER_DIR:-/opt/omni_studio/server}/setup.sh"
    if [ ! -f "$setup_sh" ]; then
        echo "ERROR: setup.sh not found at $setup_sh"
        return 1
    fi
    echo "Re-running base venv setup..."
    bash "$setup_sh" --repair-venv-only
}


# ---------------------------------------------------------------------------
# Helper: prune local caches
# ---------------------------------------------------------------------------
prune_caches() {
    echo "Purging pip cache..."
    "$PIP" cache purge || true
    echo "Cleaning $CACHE_DIR/tmp..."
    if [ -d "$CACHE_DIR/tmp" ]; then
        find "$CACHE_DIR/tmp" -mindepth 1 -delete 2>/dev/null || true
    fi
    echo "Removing empty pycache subdirectories..."
    if [ -d "$CACHE_DIR/pycache" ]; then
        find "$CACHE_DIR/pycache" -depth -type d -empty -delete 2>/dev/null || true
    fi
    if command -v huggingface-cli >/dev/null 2>&1; then
        echo "HF cache summary:"
        huggingface-cli scan-cache 2>/dev/null || true
    fi
    echo "Cache prune complete."
}


# ---------------------------------------------------------------------------
# Helper: verify safetensors shards for installed omni models
# ---------------------------------------------------------------------------
verify_models() {
    local one_model="${1:-}"
    # Pass values via env vars to avoid Python source interpolation (which
    # would let an attacker close the single-quote and inject code).
    export VERIFY_MODELS_DIR="$MODELS_DIR"
    export VERIFY_TARGET="$one_model"
    local verify_status=0
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" -c '
import json, os, sys
from pathlib import Path
from snapshot_install import verify_installed_models
report = verify_installed_models(
    Path(os.environ["VERIFY_MODELS_DIR"]) / "omni",
    target=os.environ.get("VERIFY_TARGET") or None,
)
print(json.dumps(report, indent=2))
sys.exit(1 if report["broken"] else 0)
' || verify_status=$?
    unset VERIFY_MODELS_DIR VERIFY_TARGET
    return "$verify_status"
}


# ---------------------------------------------------------------------------
# Audio Lab installers (Stable Audio + CLAP)
# ---------------------------------------------------------------------------

# Read one field from STABLE_AUDIO_MODELS / STABLE_AUDIO_VAES / CLAP_MODELS.
# Usage: audio_lab_field <registry> <variant_id> <field>
#   registry ∈ {models, vaes, clap}
audio_lab_field() {
    local registry="$1"
    local variant="$2"
    local field="$3"
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - "$registry" "$variant" "$field" <<'PYEOF'
import sys
from config import STABLE_AUDIO_MODELS, STABLE_AUDIO_VAES, CLAP_MODELS
registries = {"models": STABLE_AUDIO_MODELS, "vaes": STABLE_AUDIO_VAES, "clap": CLAP_MODELS}
reg = registries.get(sys.argv[1])
if reg is None:
    sys.exit(2)
v = reg.get(sys.argv[2])
if v is None:
    sys.exit(3)
val = v.get(sys.argv[3])
if val is None:
    sys.exit(0)
print(val)
PYEOF
}

# Generic Audio Lab snapshot download. Writes to MODELS_DIR/audio_lab/<subdir>/.
# Drops a .install_complete sentinel containing the source repo id.
# Args: <repo> <subdir> <declared_gb>
audio_lab_snapshot_download() {
    local repo="$1"
    local subdir="$2"
    local declared_gb="${3:-5}"
    local required_gb
    required_gb="$(download_required_gb "$declared_gb")"

    echo ""
    echo "Downloading: $repo → audio_lab/$subdir"
    echo "Model page: https://huggingface.co/$repo"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        echo "Using saved HuggingFace token"
    fi
    ensure_free_space "$required_gb" "audio_lab/$subdir"

    export HF_REPO="$repo"
    export HF_SUBDIR="$subdir"
    export HF_MODELS_DIR="$MODELS_DIR"

    "$PYTHON" -c "
import os, gc, sys, json
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
subdir = os.environ['HF_SUBDIR']
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
os.environ['HF_XET_CACHE'] = os.path.join(models_dir, 'xet')
os.environ['TORCH_HOME'] = os.path.join(models_dir, 'torch')
os.environ['TRANSFORMERS_CACHE'] = os.path.join(models_dir, 'hub')
from hf_download import snapshot_download_retry as snapshot_download
from snapshot_install import (
    commit_staged_directory, inspect_snapshot, staging_directory,
    write_install_complete,
)
try:
    from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
except ImportError:
    # Older huggingface_hub re-exports may differ; fall back gracefully.
    GatedRepoError = RepositoryNotFoundError = None  # type: ignore
token = os.environ.get('HF_TOKEN') or None
def _hf_token_problem():
    if not token:
        return 'No HuggingFace token is saved in Setup.'
    try:
        from huggingface_hub import HfApi
        HfApi(token=token).whoami()
        return None
    except Exception as auth_e:
        msg = str(auth_e) or repr(auth_e)
        return f'The saved HuggingFace token was rejected by HuggingFace: {msg[:180]}'

audio_root = os.path.abspath(os.path.join(models_dir, 'audio_lab'))
target_dir = os.path.abspath(os.path.join(audio_root, subdir))
local_dir = os.path.abspath(str(staging_directory(target_dir)))
try:
    common = os.path.commonpath([audio_root, target_dir])
except ValueError:
    common = ''
if common != audio_root:
    print(f'ERROR: audio_lab target escapes model root: {subdir}', file=sys.stderr)
    sys.exit(2)
os.makedirs(local_dir, exist_ok=True)
max_workers = max(1, int(os.environ.get('OMNI_DOWNLOAD_WORKERS', '4')))
try:
    snapshot_download(
        repo_id=repo,
        cache_dir=os.path.join(models_dir, 'hub'),
        local_dir=local_dir,
        token=token,
        max_workers=max_workers,
    )
except Exception as e:
    if GatedRepoError is not None and isinstance(e, GatedRepoError):
        token_problem = _hf_token_problem()
        if token_problem:
            print(f'ERROR: Saved HuggingFace token cannot access {repo}.', file=sys.stderr)
            print(f'  {token_problem}', file=sys.stderr)
            print(f'  Replace it in Setup with a current token from the account that has access.', file=sys.stderr)
        else:
            print(f'ERROR: Repo {repo} is gated.', file=sys.stderr)
            print(f'  Visit https://huggingface.co/{repo} and accept the model card,', file=sys.stderr)
            print(f'  then ensure your HF token is saved in the Setup tab.', file=sys.stderr)
        sys.exit(4)
    if RepositoryNotFoundError is not None and isinstance(e, RepositoryNotFoundError):
        print(f'ERROR: Repo {repo} not found, or requires authorization.', file=sys.stderr)
        print(f'  If this repo is gated, ensure your HF token is saved and you have', file=sys.stderr)
        print(f'  accepted the model card at https://huggingface.co/{repo}.', file=sys.stderr)
        sys.exit(4)
    msg = str(e)
    # Last-ditch heuristic for older HF clients that wrap auth failures in
    # generic HTTPError. Restrict to phrases that almost-always mean
    # authorization rather than transient network failures.
    if ('401' in msg or '403' in msg
        or 'gated' in msg.lower()
        or 'access denied' in msg.lower()
        or 'requires authorization' in msg.lower()):
        token_problem = _hf_token_problem()
        if token_problem:
            print(f'ERROR: Saved HuggingFace token cannot access {repo}.', file=sys.stderr)
            print(f'  {token_problem}', file=sys.stderr)
            print(f'  Replace it in Setup with a current token from the account that has access.', file=sys.stderr)
        else:
            print(f'ERROR: Access denied to {repo}.', file=sys.stderr)
            print(f'  The model may be gated. Visit https://huggingface.co/{repo} and', file=sys.stderr)
            print(f'  accept the model card, then ensure your HF token is saved.', file=sys.stderr)
        sys.exit(4)
    raise
gc.collect()

index_names = {
    'model.safetensors.index.json',
    'pytorch_model.bin.index.json',
    'diffusion_pytorch_model.safetensors.index.json',
    'diffusion_pytorch_model.bin.index.json',
}
checked_indexes = 0
for root, _dirs, files in os.walk(local_dir):
    for filename in files:
        if filename not in index_names:
            continue
        checked_indexes += 1
        idx_file = os.path.join(root, filename)
        try:
            with open(idx_file, 'r', encoding='utf-8') as fh:
                idx = json.load(fh)
            expected = sorted(set((idx.get('weight_map') or {}).values()))
        except Exception as e:
            print(f'ERROR: could not parse shard index {idx_file}: {e}', file=sys.stderr)
            sys.exit(1)
        missing = [s for s in expected if not os.path.exists(os.path.join(root, s))]
        if missing:
            print(f'ERROR: {len(missing)} weight shard(s) missing for {idx_file}:', file=sys.stderr)
            for m in missing[:100]:
                print(f'  {m}', file=sys.stderr)
            sys.exit(1)
if checked_indexes:
    print(f'Verified: {checked_indexes} shard index file(s)')

weight_exts = ('.safetensors', '.bin', '.pt', '.pth', '.ckpt')
has_weights = any(
    name.endswith(weight_exts)
    for _root, _dirs, files in os.walk(local_dir)
    for name in files
)
if not has_weights:
    print(f'ERROR: no model/adapter weight files found in {local_dir}', file=sys.stderr)
    sys.exit(1)

integrity = inspect_snapshot(local_dir, require_weights=True)
if not integrity['valid']:
    print('ERROR: downloaded Audio Lab snapshot failed integrity checks:', file=sys.stderr)
    for blocker in integrity['blockers'][:100]:
        print(f'  {blocker}', file=sys.stderr)
    sys.exit(1)
write_install_complete(local_dir, repo)
commit_staged_directory(local_dir, target_dir)
print(f'Download complete: audio_lab/{subdir}')
"
}

install_audio_model() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: audio-model requires <variant_id>"
        exit 1
    fi
    local repo subdir size_gb
    repo="$(audio_lab_field models "$variant" repo)"
    subdir="$(audio_lab_field models "$variant" weights_dir)"
    size_gb="$(audio_lab_field models "$variant" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown Stable Audio variant: $variant"
        exit 1
    fi
    audio_lab_snapshot_download "$repo" "$subdir" "${size_gb:-5}"
}

install_audio_vae() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: audio-vae requires <variant_id>"
        exit 1
    fi
    if [ "$variant" = "default" ]; then
        echo "default VAE — nothing to download"
        return 0
    fi
    local repo subdir size_gb
    repo="$(audio_lab_field vaes "$variant" repo)"
    subdir="$(audio_lab_field vaes "$variant" weights_dir)"
    size_gb="$(audio_lab_field vaes "$variant" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown VAE variant: $variant"
        exit 1
    fi
    audio_lab_snapshot_download "$repo" "$subdir" "${size_gb:-2}"
}

install_clap() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: clap-model requires <variant_id>"
        exit 1
    fi
    local repo subdir size_gb
    repo="$(audio_lab_field clap "$variant" repo)"
    subdir="$(audio_lab_field clap "$variant" weights_dir)"
    size_gb="$(audio_lab_field clap "$variant" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown CLAP variant: $variant"
        exit 1
    fi
    audio_lab_snapshot_download "$repo" "$subdir" "${size_gb:-2}"
}

# audio-custom <repo> [name] [kind]
install_audio_custom() {
    local repo="$1"
    local name="$2"
    local kind="${3:-model}"
    if [ -z "$repo" ]; then
        echo "ERROR: audio-custom requires <repo>"
        exit 1
    fi
    if ! [[ "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$ ]]; then
        echo "ERROR: invalid HuggingFace repo id: $repo"
        exit 1
    fi
    if [ -z "$name" ]; then
        name="$(echo "$repo" | tr '/' '_' | tr -cd 'A-Za-z0-9._-')"
    fi
    if ! [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
        echo "ERROR: invalid audio-custom local name: $name"
        exit 1
    fi
    case "$kind" in
        model|vae|clap) ;;
        *) echo "ERROR: kind must be one of: model, vae, clap (got: $kind)"; exit 1 ;;
    esac
    audio_lab_snapshot_download "$repo" "custom/$kind/$name" 5
}

# Print JSON status of all Audio Lab assets (registry + custom).
list_audio_lab_assets() {
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - <<'PYEOF'
import json
from config import (
    AUDIO_LAB_ROOT, STABLE_AUDIO_MODELS, STABLE_AUDIO_VAES, CLAP_MODELS,
    is_audio_lab_variant_installed, is_audio_lab_vae_installed, is_clap_installed,
)

def block(vid, v, installed):
    return {
        "variant_id": vid, "display": v["display"], "repo": v.get("repo"),
        "size_gb": v.get("size_gb"), "format": v.get("format"),
        "tier": v.get("tier"), "tags": v.get("tags", []),
        "installed": installed,
    }

custom_root = AUDIO_LAB_ROOT / "custom"
custom = {"model": [], "vae": [], "clap": []}
weight_exts = (".safetensors", ".bin", ".pt", ".pth", ".ckpt")

def has_weights(path):
    try:
        return any(
            f.is_file() and f.suffix.lower() in weight_exts
            for f in path.rglob("*")
        )
    except OSError:
        return False

if custom_root.exists():
    for kind_dir in custom_root.iterdir():
        if kind_dir.name in custom and kind_dir.is_dir():
            for sub in kind_dir.iterdir():
                if not sub.is_dir():
                    continue
                if not (sub / ".install_complete").exists():
                    continue
                if not has_weights(sub):
                    continue
                try:
                    repo_line = (sub / ".install_complete").read_text().strip()
                except OSError:
                    repo_line = ""
                custom[kind_dir.name].append({"name": sub.name, "repo": repo_line, "path": str(sub)})

print(json.dumps({
    "models": [block(v_id, v, is_audio_lab_variant_installed(v_id))
               for v_id, v in STABLE_AUDIO_MODELS.items()],
    "vaes":   [block(v_id, v, is_audio_lab_vae_installed(v_id))
               for v_id, v in STABLE_AUDIO_VAES.items()],
    "claps":  [block(v_id, v, is_clap_installed(v_id))
               for v_id, v in CLAP_MODELS.items()],
    "custom": custom,
}, indent=2))
PYEOF
}


# ---------------------------------------------------------------------------
# ACE-Step installers (DiT song generation, 1.5 family)
# ---------------------------------------------------------------------------

# Read one field from an ACE_STEP_* registry.
# Usage: ace_step_field <registry> <key> <field>
#   registry ∈ {models, lms, vaes, loras}
ace_step_field() {
    local registry="$1"
    local key="$2"
    local field="$3"
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - "$registry" "$key" "$field" <<'PYEOF'
import sys
from config import ACE_STEP_MODELS, ACE_STEP_LMS, ACE_STEP_VAES, ACE_STEP_LORAS
registries = {
    "models": ACE_STEP_MODELS,
    "lms":    ACE_STEP_LMS,
    "vaes":   ACE_STEP_VAES,
    "loras":  ACE_STEP_LORAS,
}
reg = registries.get(sys.argv[1])
if reg is None:
    sys.exit(2)
v = reg.get(sys.argv[2])
if v is None:
    sys.exit(3)
val = v.get(sys.argv[3])
if val is None:
    sys.exit(0)
print(val)
PYEOF
}

ace_step_installed_flag() {
    local kind="$1"
    local key="$2"
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - "$kind" "$key" <<'PYEOF'
import sys
from config import (
    ace_step_core_status,
    is_ace_step_lm_installed,
    is_ace_step_model_installed,
)
kind, key = sys.argv[1], sys.argv[2]
if kind == "core":
    ok = bool(ace_step_core_status().get("core_ready"))
elif kind == "model":
    ok = is_ace_step_model_installed(key)
elif kind == "lm":
    ok = is_ace_step_lm_installed(key)
else:
    ok = False
print("1" if ok else "0")
PYEOF
}

# Generic ACE-Step snapshot download. Writes to MODELS_DIR/ace_step/<subdir>/
# and drops a .install_complete sentinel containing the source repo id.
# Args: <repo> <subdir> <declared_gb>
# (Parallel to audio_lab_snapshot_download — kept distinct to avoid disturbing
# the audited Audio Lab install path; consolidate into a shared helper later.)
ace_step_snapshot_download() {
    local repo="$1"
    local subdir="$2"
    local declared_gb="${3:-5}"
    local required_gb
    required_gb="$(download_required_gb "$declared_gb")"

    echo ""
    echo "Downloading: $repo → ace_step/$subdir"

    HF_TOKEN_FILE="/opt/omni_studio/hf_token"
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        echo "Using saved HuggingFace token"
    fi
    ensure_free_space "$required_gb" "ace_step/$subdir"

    export HF_REPO="$repo"
    export HF_SUBDIR="$subdir"
    export HF_MODELS_DIR="$MODELS_DIR"

    "$PYTHON" -c "
import os, gc, sys
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
subdir = os.environ['HF_SUBDIR']
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
os.environ['HF_XET_CACHE'] = os.path.join(models_dir, 'xet')
os.environ['TORCH_HOME'] = os.path.join(models_dir, 'torch')
os.environ['TRANSFORMERS_CACHE'] = os.path.join(models_dir, 'hub')
from hf_download import snapshot_download_retry as snapshot_download
from snapshot_install import (
    commit_staged_directory, inspect_snapshot, staging_directory,
    write_install_complete,
)
try:
    from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
except ImportError:
    GatedRepoError = RepositoryNotFoundError = None  # type: ignore
token = os.environ.get('HF_TOKEN') or None
def _hf_token_problem():
    if not token:
        return 'No HuggingFace token is saved in Setup.'
    try:
        from huggingface_hub import HfApi
        HfApi(token=token).whoami()
        return None
    except Exception as auth_e:
        msg = str(auth_e) or repr(auth_e)
        return f'The saved HuggingFace token was rejected by HuggingFace: {msg[:180]}'

target_dir = os.path.join(models_dir, 'ace_step', subdir)
local_dir = str(staging_directory(target_dir))
os.makedirs(local_dir, exist_ok=True)
max_workers = max(1, int(os.environ.get('OMNI_DOWNLOAD_WORKERS', '4')))
try:
    snapshot_download(
        repo_id=repo,
        cache_dir=os.path.join(models_dir, 'hub'),
        local_dir=local_dir,
        token=token,
        max_workers=max_workers,
    )
except Exception as e:
    if GatedRepoError is not None and isinstance(e, GatedRepoError):
        token_problem = _hf_token_problem()
        if token_problem:
            print(f'ERROR: Saved HuggingFace token cannot access {repo}.', file=sys.stderr)
            print(f'  {token_problem}', file=sys.stderr)
            print(f'  Replace it in Setup with a current token from the account that has access.', file=sys.stderr)
        else:
            print(f'ERROR: Repo {repo} is gated.', file=sys.stderr)
            print(f'  Visit https://huggingface.co/{repo} and accept the model card,', file=sys.stderr)
            print(f'  then ensure your HF token is saved in the Setup tab.', file=sys.stderr)
        sys.exit(4)
    if RepositoryNotFoundError is not None and isinstance(e, RepositoryNotFoundError):
        print(f'ERROR: Repo {repo} not found, or requires authorization.', file=sys.stderr)
        print(f'  If gated, save your HF token and accept the card at', file=sys.stderr)
        print(f'  https://huggingface.co/{repo}.', file=sys.stderr)
        sys.exit(4)
    msg = str(e)
    if ('401' in msg or '403' in msg
        or 'gated' in msg.lower()
        or 'access denied' in msg.lower()
        or 'requires authorization' in msg.lower()):
        token_problem = _hf_token_problem()
        if token_problem:
            print(f'ERROR: Saved HuggingFace token cannot access {repo}.', file=sys.stderr)
            print(f'  {token_problem}', file=sys.stderr)
            print(f'  Replace it in Setup with a current token from the account that has access.', file=sys.stderr)
        else:
            print(f'ERROR: Access denied to {repo}.', file=sys.stderr)
            print(f'  The model may be gated. Visit https://huggingface.co/{repo} and', file=sys.stderr)
            print(f'  accept the model card, then ensure your HF token is saved.', file=sys.stderr)
        sys.exit(4)
    raise
gc.collect()
integrity = inspect_snapshot(local_dir, require_weights=True)
if not integrity['valid']:
    print('ERROR: downloaded ACE-Step snapshot failed integrity checks:', file=sys.stderr)
    for blocker in integrity['blockers'][:100]:
        print(f'  {blocker}', file=sys.stderr)
    sys.exit(1)
write_install_complete(local_dir, repo)
commit_staged_directory(local_dir, target_dir)
print(f'Download complete: ace_step/{subdir}')
"
}

install_ace_core_bundle() {
    if [ "$(ace_step_installed_flag core core)" = "1" ]; then
        echo "ACE-Step shared v1.5 core already installed"
        return 0
    fi

    local repo subdir size_gb
    repo="$(ace_step_field models "ace-1.5" repo)"
    subdir="$(ace_step_field models "ace-1.5" weights_dir)"
    size_gb="$(ace_step_field models "ace-1.5" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: ACE-Step shared core registry entry ace-1.5 is missing"
        exit 1
    fi
    echo "Installing ACE-Step shared v1.5 core bundle (ace-1.5)"
    ace_step_snapshot_download "$repo" "$subdir" "${size_gb:-12}"
}

ensure_ace_lm_installed() {
    local lm_variant="$1"
    if [ -z "$lm_variant" ]; then
        return 0
    fi
    if [ "$(ace_step_installed_flag lm "$lm_variant")" = "1" ]; then
        echo "ACE-Step recommended LM already available: $lm_variant"
        return 0
    fi
    echo "Installing ACE-Step recommended LM: $lm_variant"
    install_ace_lm "$lm_variant"
}

install_ace_model() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: ace-model requires <variant_id>"
        exit 1
    fi
    local repo subdir size_gb format default_lm
    repo="$(ace_step_field models "$variant" repo)"
    subdir="$(ace_step_field models "$variant" weights_dir)"
    size_gb="$(ace_step_field models "$variant" size_gb)"
    format="$(ace_step_field models "$variant" format)"
    default_lm="$(ace_step_field models "$variant" default_lm)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown ACE-Step model variant: $variant"
        exit 1
    fi

    if [ "$format" = "native" ] && [ "$variant" != "ace-1.5" ]; then
        install_ace_core_bundle
    fi

    ace_step_snapshot_download "$repo" "$subdir" "${size_gb:-19}"
    ensure_ace_lm_installed "$default_lm"
}

install_ace_lm() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: ace-lm requires <variant_id>"
        exit 1
    fi
    local repo subdir size_gb
    repo="$(ace_step_field lms "$variant" repo)"
    subdir="$(ace_step_field lms "$variant" weights_dir)"
    size_gb="$(ace_step_field lms "$variant" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown ACE-Step LM variant: $variant"
        exit 1
    fi
    ace_step_snapshot_download "$repo" "$subdir" "${size_gb:-4}"
}

install_ace_vae() {
    local variant="$1"
    if [ -z "$variant" ]; then
        echo "ERROR: ace-vae requires <variant_id>"
        exit 1
    fi
    if [ "$variant" = "default" ]; then
        echo "default VAE — nothing to download"
        return 0
    fi
    local repo subdir size_gb
    repo="$(ace_step_field vaes "$variant" repo)"
    subdir="$(ace_step_field vaes "$variant" weights_dir)"
    size_gb="$(ace_step_field vaes "$variant" size_gb)"
    if [ -z "$repo" ] || [ -z "$subdir" ]; then
        echo "ERROR: unknown ACE-Step VAE variant: $variant"
        exit 1
    fi
    ace_step_snapshot_download "$repo" "$subdir" "${size_gb:-1}"
}

# ace-lora <name> [repo]
#   If <name> is in the curated registry, the repo is looked up.
#   Otherwise the caller must pass a repo (used for the install-lora endpoint
#   when the user supplies an arbitrary HF repo as a community LoRA).
install_ace_lora() {
    local name="$1"
    local override_repo="$2"
    if [ -z "$name" ]; then
        echo "ERROR: ace-lora requires <name> [repo]"
        exit 1
    fi
    local repo subdir
    if [ -n "$override_repo" ]; then
        repo="$override_repo"
        subdir="loras/$name"
    else
        repo="$(ace_step_field loras "$name" repo)"
        subdir="$(ace_step_field loras "$name" weights_dir)"
        if [ -z "$repo" ] || [ -z "$subdir" ]; then
            echo "ERROR: unknown ACE-Step LoRA: $name (pass repo as 2nd arg for ad-hoc install)"
            exit 1
        fi
    fi
    ace_step_snapshot_download "$repo" "$subdir" 2
}

# ace-custom <repo> [name] [kind]
install_ace_custom() {
    local repo="$1"
    local name="$2"
    local kind="${3:-model}"
    if [ -z "$repo" ]; then
        echo "ERROR: ace-custom requires <repo>"
        exit 1
    fi
    if [ -z "$name" ]; then
        name="$(echo "$repo" | tr '/' '_' | tr -cd 'A-Za-z0-9._-')"
    fi
    case "$kind" in
        model|lm|vae|lora) ;;
        *) echo "ERROR: kind must be one of: model, lm, vae, lora (got: $kind)"; exit 1 ;;
    esac
    ace_step_snapshot_download "$repo" "custom/$kind/$name" 5
}

# Print JSON status of all ACE-Step assets (registry + custom).
list_ace_step_assets() {
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - <<'PYEOF'
import json
from config import (
    ACE_STEP_ROOT, ACE_STEP_MODELS, ACE_STEP_LMS, ACE_STEP_VAES, ACE_STEP_LORAS,
    ace_step_core_status,
    is_ace_step_model_installed, is_ace_step_lm_installed,
    is_ace_step_vae_installed, is_ace_step_lora_installed,
)

def block(key, v, installed, key_field="variant_id"):
    return {
        key_field: key,
        "display": v["display"],
        "repo": v.get("repo"),
        "size_gb": v.get("size_gb"),
        "format": v.get("format"),
        "tier": v.get("tier"),
        "available": v.get("available", True),
        "unavailable_reason": v.get("unavailable_reason"),
        "default_lm": v.get("default_lm"),
        "enables_mode": v.get("enables_mode"),
        "supported_tasks": v.get("supported_tasks", []),
        "installed": installed,
    }

custom_root = ACE_STEP_ROOT / "custom"
custom = {"model": [], "lm": [], "vae": [], "lora": []}
if custom_root.exists():
    for kind_dir in custom_root.iterdir():
        if kind_dir.name in custom and kind_dir.is_dir():
            for sub in kind_dir.iterdir():
                if not sub.is_dir():
                    continue
                if not (sub / ".install_complete").exists():
                    continue
                try:
                    repo_line = (sub / ".install_complete").read_text().strip()
                except OSError:
                    repo_line = ""
                custom[kind_dir.name].append({"name": sub.name, "repo": repo_line, "path": str(sub)})

print(json.dumps({
    "models": [block(k, v, is_ace_step_model_installed(k))
               for k, v in ACE_STEP_MODELS.items()],
    "lms":    [block(k, v, is_ace_step_lm_installed(k))
               for k, v in ACE_STEP_LMS.items()],
    "vaes":   [block(k, v, is_ace_step_vae_installed(k))
               for k, v in ACE_STEP_VAES.items()],
    "loras":  [block(k, v, is_ace_step_lora_installed(k), key_field="name")
               for k, v in ACE_STEP_LORAS.items()],
    "custom": custom,
    **ace_step_core_status(),
}, indent=2))
PYEOF
}


# ---------------------------------------------------------------------------
# Default bundle orchestration
# ---------------------------------------------------------------------------
default_installed_flag() {
    local kind="$1"
    local key="$2"
    PYTHONPATH="${SERVER_DIR:-/opt/omni_studio/server}" "$PYTHON" - "$kind" "$key" <<'PYEOF'
import sys
from config import (
    MODELS_DIR, OVERRIDES_DIR, OMNI_MODEL_SETUP,
    is_audio_lab_variant_installed, is_clap_installed,
    is_ace_step_lm_installed, is_ace_step_model_installed,
    ace_step_core_status, ACE_STEP_MODELS,
)

weight_exts = {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf"}
kind, key = sys.argv[1], sys.argv[2]

def has_weights(path):
    try:
        return path.exists() and any(
            p.is_file() and p.suffix.lower() in weight_exts
            for p in path.rglob("*")
        )
    except OSError:
        return False

ok = False
if kind == "omni":
    info = OMNI_MODEL_SETUP.get(key) or {}
    weights_dir = info.get("weights_dir")
    override = info.get("override")
    if weights_dir:
        weights_ok = has_weights(MODELS_DIR / "omni" / weights_dir)
        override_ok = True
        if override:
            override_ok = (OVERRIDES_DIR / override / ".install_complete").exists()
        ok = weights_ok and override_ok
elif kind == "audio-model":
    ok = is_audio_lab_variant_installed(key)
elif kind == "clap-model":
    ok = is_clap_installed(key)
elif kind == "ace-default":
    model = ACE_STEP_MODELS.get(key) or {}
    default_lm = model.get("default_lm")
    ok = (
        is_ace_step_model_installed(key)
        and bool(ace_step_core_status().get("core_ready"))
        and (not default_lm or is_ace_step_lm_installed(default_lm))
    )
else:
    sys.exit(2)
print("1" if ok else "0")
PYEOF
}

install_missing_defaults() {
    local fail_count=0
    local fail_list=""

    run_default() {
        local kind="$1"
        local key="$2"
        local label="$3"
        shift 3

        echo ""
        echo "--- Default: $label ---"
        if [ "$(default_installed_flag "$kind" "$key")" = "1" ]; then
            echo "Already installed; skipping $label"
            return 0
        fi
        if "$@"; then
            echo "Installed default: $label"
            return 0
        fi
        fail_count=$((fail_count + 1))
        fail_list="$fail_list $label"
        echo "ERROR: default install failed: $label" >&2
        return 0
    }

    run_default "omni" "qwen_omni_3b" "Qwen2.5-Omni-3B" install_qwen "3b"
    run_default "omni" "qwen_omni_7b" "Qwen2.5-Omni-7B" install_qwen "7b"
    run_default "omni" "minicpm_o" "MiniCPM-o 2.6" install_minicpm
    run_default "omni" "moshi" "Moshi 7B" install_moshi
    run_default "omni" "anygpt" "AnyGPT 7B" install_anygpt
    run_default "omni" "qwen3_omni" "Qwen3-Omni 30B Instruct" install_qwen3_omni
    run_default "omni" "nemotron_nano_omni" "Nemotron Nano Omni BF16" install_nemotron_nano_omni

    local audio_variant="${OMNI_DEFAULT_AUDIO_VARIANT:-sao-open-small}"
    local clap_variant="${OMNI_DEFAULT_CLAP_VARIANT:-larger-clap-general}"
    local ace_variant="${OMNI_DEFAULT_ACE_VARIANT:-ace-xl-turbo}"
    run_default "audio-model" "$audio_variant" "Stable Audio $audio_variant" install_audio_model "$audio_variant"
    run_default "clap-model" "$clap_variant" "CLAP $clap_variant" install_clap "$clap_variant"
    run_default "ace-default" "$ace_variant" "ACE-Step $ace_variant with shared core/LM" install_ace_model "$ace_variant"

    if [ "$fail_count" -gt 0 ]; then
        echo ""
        echo "WARNING: $fail_count default install(s) failed:$fail_list" >&2
        echo "Fix the reported access/network/disk issue, then retry install-missing-defaults." >&2
        exit 1
    fi

    echo ""
    echo "All missing recommended defaults are installed."
}


# Install only the official Diffusers component layout.  The upstream repo also
# contains a duplicate SGLang checkpoint layout; downloading both would consume
# about 53.4 GiB without adding capability to this worker.
install_minimax_music3_model() {
    local variant="${1:-official-diffusers}"
    if [ "$variant" != "official-diffusers" ]; then
        echo "ERROR: unknown MiniMax Music 3 variant: $variant" >&2
        exit 1
    fi

    local target="$MODELS_DIR/minimax_music3/models/official-diffusers"
    if [ -f "$target/.install_complete" ] \
       && [ -f "$target/modular_model_index.json" ] \
       && [ -f "$target/language_model/model.safetensors.index.json" ] \
       && [ -f "$target/transformer/diffusion_pytorch_model.safetensors.index.json" ]; then
        echo "MiniMax Music 3 official Diffusers snapshot is already installed"
    else
        ensure_free_space "$(download_required_gb 28)" "minimax_music3/models/official-diffusers"
        HF_TOKEN_FILE="/opt/omni_studio/hf_token"
        if [ -f "$HF_TOKEN_FILE" ]; then
            export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
            echo "Using saved HuggingFace token"
        fi
        export HF_MODELS_DIR="$MODELS_DIR"
        export HF_TARGET="$target"
        "$PYTHON" - <<'PYEOF'
import gc
import os
import sys

sys.path.insert(0, os.environ.get("SERVER_DIR", "/opt/omni_studio/server"))
from config import MINIMAX_MUSIC3_ALLOW_PATTERNS, MINIMAX_MUSIC3_REVISION
from hf_download import snapshot_download_retry as snapshot_download
from snapshot_install import (
    commit_staged_directory,
    inspect_snapshot,
    staging_directory,
    write_install_complete,
)

repo = "MiniMaxAI/MiniMax-Music3"
models_dir = os.environ["HF_MODELS_DIR"]
target = os.environ["HF_TARGET"]
staged = str(staging_directory(target))
os.makedirs(staged, exist_ok=True)
snapshot_download(
    repo_id=repo,
    revision=MINIMAX_MUSIC3_REVISION,
    allow_patterns=list(MINIMAX_MUSIC3_ALLOW_PATTERNS),
    cache_dir=os.path.join(models_dir, "hub"),
    local_dir=staged,
    token=os.environ.get("HF_TOKEN") or None,
    max_workers=max(1, int(os.environ.get("OMNI_DOWNLOAD_WORKERS", "4"))),
)
integrity = inspect_snapshot(staged, require_weights=True)
if not integrity["valid"]:
    print("ERROR: MiniMax Music 3 snapshot failed integrity checks:", file=sys.stderr)
    for blocker in integrity["blockers"][:100]:
        print(f"  {blocker}", file=sys.stderr)
    raise SystemExit(1)
required = (
    "modular_model_index.json",
    "language_model/model.safetensors.index.json",
    "transformer/diffusion_pytorch_model.safetensors.index.json",
    "rvq_depth_decoder/diffusion_pytorch_model.safetensors",
    "vocoder/diffusion_pytorch_model.safetensors",
)
missing = [name for name in required if not os.path.isfile(os.path.join(staged, name))]
if missing:
    print("ERROR: MiniMax Music 3 snapshot is missing required files:", file=sys.stderr)
    for name in missing:
        print(f"  {name}", file=sys.stderr)
    raise SystemExit(1)
write_install_complete(staged, f"{repo}@{MINIMAX_MUSIC3_REVISION}")
commit_staged_directory(staged, target)
gc.collect()
print("MiniMax Music 3 Diffusers snapshot installed")
PYEOF
    fi

    if [ ! -f "$OVERRIDES_DIR/minimax_music3/.install_complete" ]; then
        install_override_unconstrained "minimax_music3" \
            "git+https://github.com/huggingface/diffusers.git@dafe3733fcfdbf3c48915fe77be3aef65b5d6a2d"
    else
        echo "MiniMax Music 3 Diffusers override is already installed"
    fi
}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "$MODEL" in
    install-missing-defaults|defaults)
        install_missing_defaults
        ;;
    variant)
        # Called by server: bash install_model.sh variant <repo> <weights_dir>
        REPO="${2:-}"
        LOCAL_NAME="${3:-}"
        DECLARED_GB="${4:-8}"
        if [ -z "$REPO" ] || [ -z "$LOCAL_NAME" ]; then
            echo "ERROR: variant install requires <repo> <weights_dir>"
            exit 1
        fi
        download_weights "$REPO" "$LOCAL_NAME" "$DECLARED_GB"
        ;;
    audio-model)
        # bash install_model.sh audio-model <variant_id>
        install_audio_model "${2:-}"
        ;;
    audio-vae)
        # bash install_model.sh audio-vae <variant_id>
        install_audio_vae "${2:-}"
        ;;
    clap-model)
        # bash install_model.sh clap-model <variant_id>
        install_clap "${2:-}"
        ;;
    audio-custom)
        # bash install_model.sh audio-custom <repo> [name] [kind]
        install_audio_custom "${2:-}" "${3:-}" "${4:-model}"
        ;;
    audio-list)
        # bash install_model.sh audio-list — JSON status of all audio_lab assets
        list_audio_lab_assets
        ;;
    ace-model)
        # bash install_model.sh ace-model <variant_id>
        install_ace_model "${2:-}"
        ;;
    ace-lm)
        # bash install_model.sh ace-lm <variant_id>
        install_ace_lm "${2:-}"
        ;;
    ace-vae)
        # bash install_model.sh ace-vae <variant_id>
        install_ace_vae "${2:-}"
        ;;
    ace-lora)
        # bash install_model.sh ace-lora <name> [repo]
        install_ace_lora "${2:-}" "${3:-}"
        ;;
    ace-custom)
        # bash install_model.sh ace-custom <repo> [name] [kind]
        install_ace_custom "${2:-}" "${3:-}" "${4:-model}"
        ;;
    ace-list)
        # bash install_model.sh ace-list — JSON status of all ace_step assets
        list_ace_step_assets
        ;;
    minimax-music3-model)
        # bash install_model.sh minimax-music3-model [official-diffusers]
        install_minimax_music3_model "${2:-official-diffusers}"
        ;;
    lora)
        # Called by server: bash install_model.sh lora <repo> <name>
        REPO="${2:-}"
        LOCAL_NAME="${3:-}"
        if [ -z "$REPO" ] || [ -z "$LOCAL_NAME" ]; then
            echo "ERROR: lora install requires <repo> <name>"
            exit 1
        fi
        download_lora "$REPO" "$LOCAL_NAME"
        ;;
    comfy-asset)
        # bash install_model.sh comfy-asset <category> <repo> [file] [name]
        download_comfy_asset "${2:-}" "${3:-}" "${4:-}" "${5:-}"
        ;;
    comfy-url)
        # bash install_model.sh comfy-url <category> <hf-resolve-url> [name]
        download_comfy_url "${2:-}" "${3:-}" "${4:-}"
        ;;
    comfy-blueprints)
        # bash install_model.sh comfy-blueprints [filename.json] [limit] [missing_only] [categories_csv] [templates_csv]
        download_comfy_blueprint_assets "${2:-}" "${3:-500}" "${4:-1}" "${5:-}" "${6:-}"
        ;;
    comfy-node)
        # bash install_model.sh comfy-node <repo_url> [ref]
        install_comfy_node "${2:-}" "${3:-}"
        ;;
    comfyui-update)
        # bash install_model.sh comfyui-update [ref]
        update_comfyui "${2:-}"
        ;;
    repair-venv)
        repair_venv
        ;;
    prune-caches)
        prune_caches
        ;;
    verify-models)
        verify_models "${2:-}"
        ;;
    qwen_omni_3b)
        install_qwen "3b"
        ;;
    qwen_omni_7b)
        install_qwen "7b"
        ;;
    minicpm_o)
        install_minicpm
        ;;
    moshi)
        install_moshi
        ;;
    anygpt)
        install_anygpt
        ;;
    moss_tts)
        install_moss_tts
        ;;
    moss_sfx)
        install_moss_sfx
        ;;
    qwen3_omni)
        install_qwen3_omni
        ;;
    nemotron_nano_omni)
        install_nemotron_nano_omni
        ;;
    all)
        FAIL_COUNT=0
        FAIL_LIST=""
        for m in "qwen 3b" "qwen 7b" minicpm moshi anygpt; do
            set -- $m
            echo ""
            echo "--- Installing: $* ---"
            if [ "$1" = "qwen" ]; then
                install_qwen "$2" || { FAIL_COUNT=$((FAIL_COUNT+1)); FAIL_LIST="$FAIL_LIST qwen_omni_$2"; }
            else
                "install_$1" || { FAIL_COUNT=$((FAIL_COUNT+1)); FAIL_LIST="$FAIL_LIST $1"; }
            fi
        done
        if [ "$FAIL_COUNT" -gt 0 ]; then
            echo ""
            echo "WARNING: $FAIL_COUNT model(s) failed:$FAIL_LIST"
            echo "Re-run individual models to retry."
            exit 1
        fi
        ;;
    *)
        echo "ERROR: Unknown command: $MODEL"
        echo "See '$0' (no args) for usage"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "  Model $MODEL installed successfully!"
echo "============================================"
