#!/usr/bin/env bash
# =============================================================================
# Omni Studio -- Master Setup Script
# Called by the Linux Template during first run inside WSL2.
# Installs: shared Python venv, ComfyUI, ComfyUI Manager, shared deps for omni.
# =============================================================================

set -eo pipefail

REPAIR_VENV_ONLY=false
if [ "${1:-}" = "--repair-venv-only" ]; then
    REPAIR_VENV_ONLY=true
    shift
fi

OPT_DIR="/opt/omni_studio"
CACHE_DIR="$OPT_DIR/cache"
RUNTIME_DIR="$CACHE_DIR/runtime"
mkdir -p "$RUNTIME_DIR"

# Concurrency lock, kept app-scoped inside the WSL filesystem.
LOCK_FILE="$RUNTIME_DIR/setup.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: Another setup process is already running"
    exit 1
fi

DEBUG_LOG="$RUNTIME_DIR/setup.log"
exec > >(tee -a "$DEBUG_LOG") 2>&1

echo "============================================"
echo "  Omni Studio -- Environment Setup"
echo "  $(date)"
echo "============================================"
echo "Debug log: $DEBUG_LOG"
echo "User: $(whoami) / UID=$(id -u)"
echo "Kernel: $(uname -r)"

# ---------------------------------------------------------------------------
# Step 1: Detect paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_APP_DIR="$(dirname "$SCRIPT_DIR")"
APP_DIR="$OPT_DIR"

echo "Server code: $SCRIPT_DIR"
echo "Source app directory: $SOURCE_APP_DIR"
echo "Runtime app directory: $APP_DIR"

VENV_DIR="$OPT_DIR/venv"
OVERRIDES_DIR="$OPT_DIR/overrides"
COMFYUI_DIR="$OPT_DIR/comfyui"
SERVER_DIR="$OPT_DIR/server"
MODELS_DIR="$OPT_DIR/models"
OUTPUT_DIR="$OPT_DIR/output"
WORKFLOWS_DIR="$OPT_DIR/workflows"
PERSIST_DIR="${OMNI_PERSIST_DIR:-/var/lib/omni_studio}"
PID_DIR="$RUNTIME_DIR/pids"
API_TOKEN_FILE="$RUNTIME_DIR/api_token"
COMFYUI_REF="${OMNI_COMFYUI_REF:-1ac60da2c9c8f83654204b2a1db13908cf7614f7}"
COMFYUI_MANAGER_REF="${OMNI_COMFYUI_MANAGER_REF:-7955e638db7d4a4b8bf7a614e724a2013b83dfd7}"
PY_CONSTRAINTS="$SCRIPT_DIR/python_constraints.txt"

CPU_TOTAL="$(nproc 2>/dev/null || echo 2)"
OMNI_CPU_WORKERS="${OMNI_CPU_WORKERS:-$(( (CPU_TOTAL + 1) / 2 ))}"
if [ "$OMNI_CPU_WORKERS" -lt 1 ]; then
    OMNI_CPU_WORKERS=1
fi
OMNI_DOWNLOAD_WORKERS="${OMNI_DOWNLOAD_WORKERS:-$(( CPU_TOTAL / 3 ))}"
if [ "$OMNI_DOWNLOAD_WORKERS" -lt 1 ]; then
    OMNI_DOWNLOAD_WORKERS=1
fi
OMNI_MAX_CONCURRENT_DOWNLOADS="${OMNI_MAX_CONCURRENT_DOWNLOADS:-3}"

# Keep irreplaceable/user-created state outside the replaceable /opt runtime
# tree. The public paths do not change: callers and ComfyUI still resolve
# /opt/omni_studio/... while the ext4-backed targets survive a runtime-tree
# rebuild. Refuse to merge two populated trees automatically; silently choosing
# either side could lose models or user outputs.
ensure_persistent_link() {
    local live_path="$1"
    local persistent_path="$2"
    local label="$3"

    mkdir -p "$(dirname "$live_path")" "$(dirname "$persistent_path")"

    if [ -L "$live_path" ]; then
        local resolved
        resolved="$(readlink -f "$live_path" 2>/dev/null || true)"
        if [ "$resolved" != "$persistent_path" ]; then
            echo "ERROR: $label link points to $resolved, expected $persistent_path"
            exit 1
        fi
        mkdir -p "$persistent_path"
        return
    fi

    if [ -d "$live_path" ] && [ -n "$(find "$live_path" -mindepth 1 -print -quit)" ]; then
        if [ -d "$persistent_path" ] \
           && [ -n "$(find "$persistent_path" -mindepth 1 -print -quit)" ]; then
            echo "ERROR: Both $label trees contain data; refusing an implicit merge:"
            echo "  runtime: $live_path"
            echo "  persistent: $persistent_path"
            exit 1
        fi
        rmdir "$persistent_path" 2>/dev/null || true
        mv "$live_path" "$persistent_path"
    else
        rmdir "$live_path" 2>/dev/null || true
        mkdir -p "$persistent_path"
    fi

    ln -s "$persistent_path" "$live_path"
    echo "Persistent $label: $live_path -> $persistent_path"
}

ensure_persistent_link "$MODELS_DIR" "$PERSIST_DIR/models" "Omni model cache"
ensure_persistent_link "$OUTPUT_DIR" "$PERSIST_DIR/output" "output library"
ensure_persistent_link "$WORKFLOWS_DIR" "$PERSIST_DIR/workflows" "workflow library"

ensure_comfy_persistent_state() {
    ensure_persistent_link "$COMFYUI_DIR/models" "$PERSIST_DIR/comfyui/models" "ComfyUI models"
    ensure_persistent_link "$COMFYUI_DIR/input" "$PERSIST_DIR/comfyui/input" "ComfyUI inputs"
    ensure_persistent_link "$COMFYUI_DIR/user" "$PERSIST_DIR/comfyui/user" "ComfyUI user data"
}

# Existing/snapshotted installations can take the fast-start exit below, so
# establish their state links before evaluating that gate.
if [ -f "$COMFYUI_DIR/main.py" ]; then
    ensure_comfy_persistent_state
fi

mkdir -p \
    "$OPT_DIR" "$OVERRIDES_DIR" \
    "$MODELS_DIR/omni" "$MODELS_DIR/hub" "$MODELS_DIR/xet" "$MODELS_DIR/torch" \
    "$MODELS_DIR/lora" "$MODELS_DIR/hf_datasets" \
    "$OUTPUT_DIR/logs" "$OUTPUT_DIR/comfyui" "$OUTPUT_DIR/comfyui_input" \
    "$WORKFLOWS_DIR" "$CACHE_DIR/pip" "$CACHE_DIR/xdg" "$CACHE_DIR/tmp" \
    "$CACHE_DIR/pycache" "$CACHE_DIR/comfyui_temp" "$CACHE_DIR/torch_extensions" \
    "$CACHE_DIR/triton" "$CACHE_DIR/cuda" "$CACHE_DIR/numba" \
    "$CACHE_DIR/matplotlib" "$CACHE_DIR/thumbs" "$RUNTIME_DIR" "$PID_DIR"

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
# Import-time TorchInductor probes can otherwise spawn a worker per logical
# core during setup, competing with the desktop and unrelated user workloads.
export TORCHINDUCTOR_COMPILE_THREADS="${OMNI_TORCH_COMPILE_THREADS:-1}"
export OMP_NUM_THREADS="$OMNI_CPU_WORKERS"
export OPENBLAS_NUM_THREADS="$OMNI_CPU_WORKERS"
export MKL_NUM_THREADS="$OMNI_CPU_WORKERS"
export NUMEXPR_NUM_THREADS="$OMNI_CPU_WORKERS"
export HF_XET_NUM_CONCURRENT_RANGE_GETS="$OMNI_DOWNLOAD_WORKERS"
export OMNI_CPU_WORKERS
export OMNI_DOWNLOAD_WORKERS
export OMNI_MAX_CONCURRENT_DOWNLOADS
export OMNI_APP_INSTANCE="$APP_DIR"

# Write config for Python server
_env_tmp="$OPT_DIR/env.conf.tmp.$$"
cat > "$_env_tmp" << EOF
APP_DIR="$APP_DIR"
SOURCE_APP_DIR="$SOURCE_APP_DIR"
SERVER_DIR="$SERVER_DIR"
MODELS_DIR="$MODELS_DIR"
OUTPUT_DIR="$OUTPUT_DIR"
WORKFLOWS_DIR="$WORKFLOWS_DIR"
VENV_DIR="$VENV_DIR"
OVERRIDES_DIR="$OVERRIDES_DIR"
COMFYUI_DIR="$COMFYUI_DIR"
CACHE_DIR="$CACHE_DIR"
RUNTIME_DIR="$RUNTIME_DIR"
PID_DIR="$PID_DIR"
API_TOKEN_FILE="$API_TOKEN_FILE"
OMNI_CPU_WORKERS="$OMNI_CPU_WORKERS"
OMNI_DOWNLOAD_WORKERS="$OMNI_DOWNLOAD_WORKERS"
OMNI_MAX_CONCURRENT_DOWNLOADS="$OMNI_MAX_CONCURRENT_DOWNLOADS"
OMNI_APP_INSTANCE="$APP_DIR"
EOF
mv -f "$_env_tmp" "$OPT_DIR/env.conf"
echo "Environment config written to $OPT_DIR/env.conf"

# Copy server code into WSL ext4. Runtime must not execute through /mnt.
case "$SERVER_DIR" in
    "$OPT_DIR"/server) ;;
    *) echo "ERROR: Refusing to update unexpected SERVER_DIR=$SERVER_DIR"; exit 1 ;;
esac
rm -rf "$SERVER_DIR.new"
mkdir -p "$SERVER_DIR.new"
cp -a "$SCRIPT_DIR"/. "$SERVER_DIR.new"/
find "$SERVER_DIR.new" -type d -exec chmod 755 {} +
find "$SERVER_DIR.new" -type f -exec chmod 644 {} +
find "$SERVER_DIR.new" -type f -name '*.sh' -exec chmod 755 {} +
if [ -e "$SERVER_DIR" ] || [ -L "$SERVER_DIR" ]; then
    rm -rf "$SERVER_DIR"
fi
mv "$SERVER_DIR.new" "$SERVER_DIR"
echo "Copied server code to WSL ext4: $SERVER_DIR"

if [ -f "$SOURCE_APP_DIR/bridge.py" ]; then
    cp "$SOURCE_APP_DIR/bridge.py" "$OPT_DIR/bridge.py"
    chmod 755 "$OPT_DIR/bridge.py"
    echo "Copied bridge launcher to WSL ext4: $OPT_DIR/bridge.py"
else
    echo "ERROR: Missing bridge launcher at $SOURCE_APP_DIR/bridge.py"
    exit 1
fi

if [ -f "$SOURCE_APP_DIR/windows_loopback_relay.py" ]; then
    cp "$SOURCE_APP_DIR/windows_loopback_relay.py" "$OPT_DIR/windows_loopback_relay.py"
    chmod 755 "$OPT_DIR/windows_loopback_relay.py"
    echo "Copied Windows loopback relay to WSL ext4: $OPT_DIR/windows_loopback_relay.py"
else
    echo "ERROR: Missing Windows loopback relay at $SOURCE_APP_DIR/windows_loopback_relay.py"
    exit 1
fi

# Keep the runtime's agent/API guidance in lockstep with the child source.
# These trees are app-owned documentation, not user data.
sync_runtime_tree() {
    local name="$1"
    local source="$SOURCE_APP_DIR/$name"
    local staging="$OPT_DIR/$name.new"
    local destination="$OPT_DIR/$name"
    if [ ! -d "$source" ]; then
        echo "ERROR: Missing runtime guidance tree at $source"
        exit 1
    fi
    rm -rf "$staging"
    mkdir -p "$staging"
    cp -a "$source"/. "$staging"/
    rm -rf "$destination"
    mv "$staging" "$destination"
}

if [ ! -f "$SOURCE_APP_DIR/AGENTS.md" ]; then
    echo "ERROR: Missing agent guidance at $SOURCE_APP_DIR/AGENTS.md"
    exit 1
fi
cp "$SOURCE_APP_DIR/AGENTS.md" "$OPT_DIR/AGENTS.md"
sync_runtime_tree "docs"
sync_runtime_tree "skills"
sync_runtime_tree "cli"
if [ ! -f "$SOURCE_APP_DIR/omni-cli" ]; then
    echo "ERROR: Missing CLI launcher at $SOURCE_APP_DIR/omni-cli"
    exit 1
fi
cp "$SOURCE_APP_DIR/omni-cli" "$OPT_DIR/omni-cli"
chmod 755 "$OPT_DIR/omni-cli"
if [ ! -e /usr/local/bin/omni-cli ] && [ ! -L /usr/local/bin/omni-cli ]; then
    ln -s "$OPT_DIR/omni-cli" /usr/local/bin/omni-cli
elif [ "$(readlink -f /usr/local/bin/omni-cli 2>/dev/null)" != "$OPT_DIR/omni-cli" ]; then
    echo "WARNING: /usr/local/bin/omni-cli belongs to another install; use $OPT_DIR/omni-cli"
fi
echo "Synchronized runtime agent and API guidance"

SETUP_READY_STAMP="$RUNTIME_DIR/setup_complete.stamp"
fast_start_runtime_deps_ok() {
    "$VENV_DIR/bin/python3" - <<'PY'
import importlib.util
import sys

required = {
    "transformers": "transformers",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "diffusers": "diffusers",
    "acestep": "ACE-Step",
    "loguru": "loguru",
    "vector_quantize_pytorch": "vector-quantize-pytorch",
    "peft": "peft",
}
missing = [label for module, label in required.items()
           if importlib.util.find_spec(module) is None]
if missing:
    print("Missing Python runtime packages: " + ", ".join(missing))
    sys.exit(1)
PY
}

if [ -d "$COMFYUI_DIR/custom_nodes" ] && [ -d "$SOURCE_APP_DIR/comfy_nodes/omni_bridge" ]; then
    mkdir -p "$COMFYUI_DIR/custom_nodes/omni_bridge"
    cp -a "$SOURCE_APP_DIR/comfy_nodes/omni_bridge/." "$COMFYUI_DIR/custom_nodes/omni_bridge/"
fi

if [ "${OMNI_FAST_START:-1}" = "1" ] \
   && [ -f "$SETUP_READY_STAMP" ] \
   && [ -x "$VENV_DIR/bin/python3" ] \
   && [ -f "$SERVER_DIR/omni_comfy_server.py" ] \
   && [ -f "$COMFYUI_DIR/main.py" ]; then
    if fast_start_runtime_deps_ok; then
        echo "Fast start: setup already completed; runtime files refreshed."
        exit 0
    fi
    echo "Fast start: runtime dependency smoke check failed; repairing venv only."
    REPAIR_VENV_ONLY=true
fi

checkout_git_ref() {
    local repo="$1"
    local dest="$2"
    local ref="$3"
    if [ -d "$dest/.git" ]; then
        echo "Updating $dest to $ref..."
        git -C "$dest" fetch --depth 1 origin "$ref" || return 1
    else
        if [ -d "$dest" ]; then
            local unexpected
            unexpected="$(find "$dest" -mindepth 1 -maxdepth 1 ! -name .cache -print -quit)"
            if [ -n "$unexpected" ]; then
                echo "ERROR: Refusing to clone over non-cache content in $dest: $unexpected"
                return 1
            fi
            rm -rf "$dest/.cache"
            rmdir "$dest"
        fi
        echo "Cloning $repo to $dest..."
        git clone --filter=blob:none --no-checkout "$repo" "$dest"
        git -C "$dest" fetch --depth 1 origin "$ref" || return 1
    fi
    git -C "$dest" checkout --detach --force FETCH_HEAD || return 1
    # FETCH_HEAD comes only from the successful fetch of this exact ref.
    # Additionally verify a pinned full commit identity.
    if printf '%s' "$ref" | grep -Eq '^[0-9a-f]{40}$'; then
        local resolved
        resolved="$(git -C "$dest" rev-parse HEAD)"
        if [ "$resolved" != "$ref" ]; then
            echo "ERROR: pinned ref $ref could not be checked out in $dest" >&2
            echo "       (HEAD is $resolved -- refusing to run unpinned upstream code)" >&2
            return 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step 2: Install system dependencies
# ---------------------------------------------------------------------------
if [ "$REPAIR_VENV_ONLY" = true ]; then
    echo ""
    echo "(--repair-venv-only: skipping Step 2 system deps)"
else
echo ""
echo "=== Installing system dependencies ==="

APT_UPDATED=false

install_if_missing() {
    local cmd="$1"
    local pkg="$2"
    if ! command -v "$cmd" > /dev/null 2>&1; then
        if [ "$APT_UPDATED" = false ]; then
            echo "Running apt-get update..."
            apt-get update -qq
            APT_UPDATED=true
        fi
        echo "Installing $pkg..."
        apt-get install -y -qq "$pkg"
    else
        echo "$cmd: already installed"
    fi
}

install_if_missing "ffmpeg" "ffmpeg"
install_if_missing "git" "git"
install_if_missing "curl" "curl"
install_if_missing "espeak-ng" "espeak-ng"

for pkg in libsndfile1 libsndfile1-dev libssl-dev python3-venv python3-dev build-essential libgl1 libglib2.0-0; do
    if ! dpkg -s "$pkg" > /dev/null 2>&1; then
        if [ "$APT_UPDATED" = false ]; then
            echo "Running apt-get update..."
            apt-get update -qq
            APT_UPDATED=true
        fi
        echo "Installing $pkg..."
        apt-get install -y -qq "$pkg"
    else
        echo "$pkg: already installed"
    fi
done

fi  # end optional system dependency installation

# Check GPU and detect CUDA version
echo ""
echo "=== Checking GPU ==="
CUDA_AVAILABLE=false
CUDA_WHEEL="cpu"
if command -v nvidia-smi > /dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true
    echo "CUDA GPU detected"
    CUDA_AVAILABLE=true

    # Detect CUDA runtime version to pick correct PyTorch wheel
    CUDA_VER=""
    if command -v nvcc > /dev/null 2>&1; then
        CUDA_VER=$(nvcc --version 2>/dev/null | grep -oP 'release \K[0-9]+\.[0-9]+' | head -1)
    fi
    if [ -z "$CUDA_VER" ] && [ -f /usr/local/cuda/version.txt ]; then
        CUDA_VER=$(grep -oP '[0-9]+\.[0-9]+' /usr/local/cuda/version.txt | head -1)
    fi
    if [ -z "$CUDA_VER" ]; then
        # Fallback: parse nvidia-smi CUDA version
        CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+' | head -1)
    fi

    echo "Detected CUDA version: ${CUDA_VER:-unknown}"

    # Map CUDA version to PyTorch wheel index
    case "$CUDA_VER" in
        11.8*) CUDA_WHEEL="cu118" ;;
        12.1*) CUDA_WHEEL="cu121" ;;
        12.4*|12.5*) CUDA_WHEEL="cu124" ;;
        12.6*|12.7*) CUDA_WHEEL="cu126" ;;
        12.8*|12.9*|13.*) CUDA_WHEEL="cu128" ;;
        12.*) CUDA_WHEEL="cu126" ;;  # best-effort for other 12.x
        *) CUDA_WHEEL="cu126"; echo "WARNING: Unknown CUDA $CUDA_VER -- defaulting to cu126 wheel" ;;
    esac
    echo "Using PyTorch wheel index: $CUDA_WHEEL"
else
    echo "WARNING: nvidia-smi not found. Will run on CPU only."
fi


# ---------------------------------------------------------------------------
# Step 3: Create base virtual environment
# ---------------------------------------------------------------------------
echo ""
echo "=== Setting up Python virtual environment ==="
CUDA_WHEEL="${CUDA_WHEEL:-cpu}"

TORCH_OK=false
if [ -f "$VENV_DIR/bin/python3" ] && "$VENV_DIR/bin/python3" -c "import sys, torch; sys.exit(0 if tuple(map(int, torch.__version__.split('+')[0].split('.')[:2])) >= (2, 6) else 1)" 2>/dev/null; then
    TORCH_OK=true
    echo "Base venv already exists with PyTorch >= 2.6 -- skipping creation"
else
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
        if [ ! -x "$VENV_DIR/bin/python3" ] || [ ! -x "$VENV_DIR/bin/pip" ]; then
            echo "ERROR: venv creation failed, retrying..."
            rm -rf "$VENV_DIR"
            python3 -m venv "$VENV_DIR"
        fi
    else
        echo "Venv exists but torch is missing or too old for Qwen Omni -- repairing..."
    fi

    "$VENV_DIR/bin/pip" install --upgrade pip "setuptools<70" "wheel>=0.46.2,<0.47" -q

    # PyTorch
    if [ "$TORCH_OK" != true ]; then
        echo ""
        echo "Installing PyTorch >= 2.6 (this may take several minutes)..."
        echo "Wheel index: $CUDA_WHEEL"
        "$VENV_DIR/bin/pip" install "torch>=2.6,<2.8" "torchvision>=0.21,<0.23" "torchaudio>=2.6,<2.8" \
            --index-url "https://download.pytorch.org/whl/$CUDA_WHEEL"
        if ! "$VENV_DIR/bin/python3" -c "import sys, torch; print(f'PyTorch {torch.__version__} OK'); sys.exit(0 if tuple(map(int, torch.__version__.split('+')[0].split('.')[:2])) >= (2, 6) else 1)"; then
            echo "ERROR: PyTorch installation failed"
            exit 1
        fi
    fi

    echo "Base venv setup complete!"
fi

echo "Ensuring Python packaging tools..."
"$VENV_DIR/bin/pip" install --upgrade pip "setuptools<70" "wheel>=0.46.2,<0.47" -q
for _sp in "$VENV_DIR"/lib/python*/site-packages; do
    if [ -d "$_sp" ]; then
        rm -rf "$_sp"/~ransformers "$_sp"/~ransformers-*.dist-info 2>/dev/null || true
    fi
done
unset _sp

# Core shared dependencies (used by both ComfyUI and omni models)
# Run this on every setup so existing environments can be repaired when
# a model starts requiring newer transformers or related libraries.
echo ""
echo "Installing shared Python packages..."
"$VENV_DIR/bin/pip" install -q \
    -c "$PY_CONSTRAINTS" \
    "transformers>=4.45" \
    "accelerate>=0.26" \
    "peft>=0.10" \
    "scipy>=1.12" \
    "numpy>=1.24" \
    "librosa>=0.10" \
    "soundfile>=0.12" \
    "huggingface_hub[hf_xet]>=0.23" \
    hf_xet \
    httpx \
    "fastapi>=0.110" \
    "uvicorn>=0.29" \
    python-multipart \
    "pydantic>=2.0" \
    requests \
    tqdm \
    safetensors \
    sentencepiece \
    protobuf \
    aiohttp \
    "websockets>=13,<17" \
    "Pillow>=9.0" \
    opencv-python-headless

# Quantization packages -- need torch present, so install into
# the shared venv (not per-model overrides).
# bitsandbytes provides int4/int8 quantization (BitsAndBytesConfig).
# auto-gptq is NOT installed -- it is unmaintained and requires the full CUDA
# toolkit. Dynamic quantization uses bitsandbytes; the maintained GPTQModel
# runtime for pre-quantized Qwen variants lives in the isolated Qwen override.
echo "Installing quantization packages..."
"$VENV_DIR/bin/pip" install -q \
    -c "$PY_CONSTRAINTS" \
    "bitsandbytes>=0.43,<1.0" \
    "optimum>=1.17,<2.0"

# ---------------------------------------------------------------------------
# Audio Lab (Stable Audio + CLAP) dependencies.
# Idempotent: skipped when /opt/omni_studio/.audio_lab_deps_complete already
# contains the current OMNI_AUDIO_LAB_REV. Bump the rev to force reinstall.
# ---------------------------------------------------------------------------
AUDIO_LAB_REV="${OMNI_AUDIO_LAB_REV:-20260711_audio_lab_native_runtime_v2}"
AUDIO_LAB_STAMP="$OPT_DIR/.audio_lab_deps_complete"
if [ -f "$AUDIO_LAB_STAMP" ] && [ "$(cat "$AUDIO_LAB_STAMP" 2>/dev/null)" = "$AUDIO_LAB_REV" ]; then
    echo "Audio Lab deps already installed (rev $AUDIO_LAB_REV) — skipping"
else
    echo ""
    echo "=== Installing Audio Lab dependencies (rev $AUDIO_LAB_REV) ==="
    # stable-audio-tools pulls in encodec, descript-audio-codec, aeiou,
    # auraloss, alias-free-torch, x-transformers, prefigure, k-diffusion,
    # pytorch-lightning. Diffusers is the alternative loader path for
    # StableAudioPipeline (used for the official + Foundation-1-Diffusers
    # variants). Constrained by python_constraints.txt to prevent torch
    # downgrade or major-version drift.
    # Keep resolver scopes small. Resolving this entire optional stack in one
    # transaction can backtrack at one full CPU core for many minutes on a
    # cold environment, making the desktop and agent session unresponsive.
    echo "Audio Lab dependency group 1/3: loaders and sampling primitives"
    "$VENV_DIR/bin/pip" install -q --prefer-binary \
        -c "$PY_CONSTRAINTS" \
        "diffusers>=0.27,<0.40" \
        "einops>=0.7" \
        "einops-exts>=0.0.4,<1.0" \
        "ema-pytorch==0.2.3" \
        "local-attention==1.8.6" \
        "v-diffusion-pytorch==0.0.2"

    echo "Audio Lab dependency group 2/3: codecs and transformers"
    "$VENV_DIR/bin/pip" install -q --prefer-binary \
        -c "$PY_CONSTRAINTS" \
        "x-transformers>=1.30,<3.0" \
        "descript-audio-codec>=1.0,<2.0" \
        "encodec>=0.1.1,<1.0" \
        "alias-free-torch>=0.0.6,<1.0"

    echo "Audio Lab dependency group 3/3: losses and training helpers"
    # These optional packages have broad visualization/training dependency
    # trees. Resolve them independently so unrelated extras cannot trigger a
    # combinatorial backtrack. k-diffusion's dctorch metadata still requires
    # NumPy <2 even though the runtime works with Omni's required NumPy 2; keep
    # that one legacy metadata edge out of the resolver and verify imports.
    "$VENV_DIR/bin/pip" install -q --prefer-binary -c "$PY_CONSTRAINTS" \
        "auraloss>=0.4,<1.0"
    "$VENV_DIR/bin/pip" install -q --prefer-binary -c "$PY_CONSTRAINTS" \
        "aeiou>=0.0.20,<1.0"
    "$VENV_DIR/bin/pip" install -q --no-deps \
        "k-diffusion>=0.1.1,<1.0" "dctorch>=0.1,<1.0"
    "$VENV_DIR/bin/pip" install -q --prefer-binary -c "$PY_CONSTRAINTS" \
        clean-fid clip-anytorch jsonmerge kornia scikit-image \
        torchdiffeq torchsde
    "$VENV_DIR/bin/pip" install -q --prefer-binary -c "$PY_CONSTRAINTS" \
        "prefigure>=0.0.9,<1.0"
    "$VENV_DIR/bin/pip" install -q --prefer-binary -c "$PY_CONSTRAINTS" \
        "pytorch-lightning>=2.1,<3.0"
    "$VENV_DIR/bin/python3" -c "import dctorch, k_diffusion"

    # laion-clap's legacy metadata hard-pins NumPy 1.23.5, for which Python
    # 3.12 has no supported wheel. Stable Audio imports the package for its
    # optional conditioner registry, while Omni's scoring path uses the modern
    # transformers ClapModel. Install the pure Python package without allowing
    # its obsolete metadata to downgrade NumPy/OpenCV.
    "$VENV_DIR/bin/pip" install -q --no-deps "laion-clap==1.1.4"

    # Some transitive audio/training packages still advertise NumPy 1.x.
    # Omni's OpenCV build requires NumPy 2 on Python 3.12; restore that suite
    # invariant after resolving the optional native audio stack.
    "$VENV_DIR/bin/pip" install -q --no-deps -c "$PY_CONSTRAINTS" "numpy>=2,<3"

    if ! "$VENV_DIR/bin/python3" -c "import stable_audio_tools" 2>/dev/null; then
        "$VENV_DIR/bin/pip" install -q --no-deps "stable-audio-tools>=0.0.16,<0.1" \
            || echo "WARNING: optional stable-audio-tools install failed; native Audio Lab variants will be unavailable"
    fi

    if ! "$VENV_DIR/bin/python3" -c "
from stable_audio_tools.models.factory import create_model_from_config
from stable_audio_tools.inference.generation import generate_diffusion_cond
print('Audio Lab native imports OK')
"; then
        echo "ERROR: Audio Lab native imports failed"
        exit 1
    fi

    # Smoke-import: fail the setup loudly if anything didn't actually land,
    # rather than waiting for the first worker spawn to surface the error.
    # Exercise both the SA toolkit, diffusers, the CLAP model class, and the
    # transformers ClapProcessor — these are the actual classes the loader uses.
    if ! "$VENV_DIR/bin/python3" -c "
import diffusers
from diffusers import StableAudioPipeline   # confirm pipeline exists in this diffusers version
from transformers import ClapModel, ClapProcessor   # CLAP scoring path
print('Audio Lab imports OK')
"; then
        echo "ERROR: Audio Lab smoke import failed — see pip output above"
        exit 1
    fi
    echo "$AUDIO_LAB_REV" > "$AUDIO_LAB_STAMP"
    echo "Audio Lab deps OK."
fi

# Pre-create the audio_lab model storage tree so install/load have somewhere
# to land. Per-variant dirs are created on demand by install_model.sh.
mkdir -p \
    "$MODELS_DIR/audio_lab" \
    "$MODELS_DIR/audio_lab/clap" \
    "$MODELS_DIR/audio_lab/vae" \
    "$MODELS_DIR/audio_lab/custom" \
    "$OUTPUT_DIR/omni/audio_lab"

# ---------------------------------------------------------------------------
# ACE-Step 1.5 (DiT song generation) dependencies.
# Installed via --no-deps so ACE-Step's hard-pinned requirements.txt
# (transformers==4.50.0, pytorch_lightning==2.5.1, accelerate==1.6.0, etc.)
# does not clobber the shared venv. peft is added explicitly because it's
# required for LoRA support and may not already be present.
# Idempotent on /opt/omni_studio/.ace_step_deps_complete + OMNI_ACE_STEP_REV.
# ---------------------------------------------------------------------------
ACE_STEP_REV="${OMNI_ACE_STEP_REV:-20260628_ace_step_v15_api}"
ACE_STEP_STAMP="$OPT_DIR/.ace_step_deps_complete"
# Source pin - bump alongside ACE_STEP_REV to upgrade. ACE-Step 1.5 has changed
# public APIs on main, so keep this fixed unless the loader is updated too.
ACE_STEP_REPO="${OMNI_ACE_STEP_REPO:-git+https://github.com/ace-step/ACE-Step-1.5.git@6d467e4b5081ccb0abf1ec1bf4fdf9051a2d34b0}"
# Always smoke-test imports — even if the sentinel claims the install is OK.
# This catches the case where a user manually pip-uninstalls acestep but
# leaves the sentinel behind (which would otherwise silently bypass the next
# setup run's repair).
_ace_step_smoke_ok=0
"$VENV_DIR/bin/python3" -c "
import acestep
from acestep.handler import AceStepHandler
from acestep.inference import GenerationParams, GenerationConfig, generate_music
import peft
" 2>/dev/null && _ace_step_smoke_ok=1

if [ -f "$ACE_STEP_STAMP" ] \
   && [ "$(cat "$ACE_STEP_STAMP" 2>/dev/null)" = "$ACE_STEP_REV" ] \
   && [ "$_ace_step_smoke_ok" -eq 1 ]; then
    echo "ACE-Step deps already installed (rev $ACE_STEP_REV) — skipping"
elif [ "$_ace_step_smoke_ok" -eq 1 ]; then
    echo "ACE-Step deps import OK; refreshing sentinel for rev $ACE_STEP_REV."
    echo "$ACE_STEP_REV" > "$ACE_STEP_STAMP"
else
    if [ "$_ace_step_smoke_ok" -eq 0 ] && [ -f "$ACE_STEP_STAMP" ]; then
        echo "ACE-Step sentinel present but smoke-import failed — reinstalling."
        rm -f "$ACE_STEP_STAMP"
    fi
    echo ""
    echo "=== Installing ACE-Step dependencies (rev $ACE_STEP_REV) ==="
    # --no-deps avoids the upstream hard pins. Torch, diffusers, transformers,
    # safetensors, accelerate, librosa, soundfile, einops are already satisfied
    # by the Audio Lab block above.
    "$VENV_DIR/bin/pip" install -q --no-deps --force-reinstall "$ACE_STEP_REPO" \
        || { echo "ERROR: ACE-Step package install failed"; exit 1; }
    # peft for PEFT-style LoRA stacking; omegaconf if any acestep submodule
    # imports it (cheap, idempotent).
    "$VENV_DIR/bin/pip" install -q \
        -c "$PY_CONSTRAINTS" \
        "peft>=0.10" \
        "omegaconf>=2.3" \
        "loguru>=0.7.3" \
        "diskcache" \
        "vector-quantize-pytorch>=1.27.15" \
        "pytorch-wavelets>=1.3.0" \
        "PyWavelets>=1.9.0" \
        "modelscope" \
        "typer-slim>=0.21.1"

    # Smoke-import: verify the acestep package exposes its pipeline AND that
    # peft is importable for LoRA flows. trust_remote_code is needed at
    # *runtime* for some loader paths, not for import itself.
    if ! "$VENV_DIR/bin/python3" -c "
import acestep
from acestep.handler import AceStepHandler
from acestep.inference import GenerationParams, GenerationConfig, generate_music
import peft
print('ACE-Step imports OK')
"; then
        echo "ERROR: ACE-Step smoke import failed after install — see pip output above"
        exit 1
    fi
    echo "$ACE_STEP_REV" > "$ACE_STEP_STAMP"
    echo "ACE-Step deps OK."
fi
unset _ace_step_smoke_ok

# Pre-create the ace_step model storage tree.
mkdir -p \
    "$MODELS_DIR/ace_step" \
    "$MODELS_DIR/ace_step/models" \
    "$MODELS_DIR/ace_step/lms" \
    "$MODELS_DIR/ace_step/vaes" \
    "$MODELS_DIR/ace_step/loras" \
    "$MODELS_DIR/ace_step/custom" \
    "$OUTPUT_DIR/omni/ace_step"

if [ "$REPAIR_VENV_ONLY" = true ]; then
    echo ""
    echo "============================================"
    echo "  Venv repair complete (Steps 4-7 skipped)"
    echo "============================================"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 4: Install ComfyUI
# ---------------------------------------------------------------------------
echo ""
echo "=== Setting up ComfyUI ==="

checkout_git_ref "https://github.com/Comfy-Org/ComfyUI.git" "$COMFYUI_DIR" "$COMFYUI_REF"

ensure_comfy_persistent_state
mkdir -p \
    "$COMFYUI_DIR/.cache/huggingface/hub" "$COMFYUI_DIR/.cache/huggingface/xet" \
    "$COMFYUI_DIR/.cache/torch" "$COMFYUI_DIR/.cache/datasets"

# Install ComfyUI requirements (constrain torch so it cannot be downgraded)
echo "Installing ComfyUI requirements..."
_constraints="$(mktemp)"
trap 'rm -f "$_constraints"' EXIT
echo "torch>=2.6" > "$_constraints"
"$VENV_DIR/bin/pip" install -q -r "$COMFYUI_DIR/requirements.txt" -c "$_constraints" -c "$PY_CONSTRAINTS"
rm -f "$_constraints"
trap - EXIT

# Create model directories
echo "Creating ComfyUI model directories..."
COMFY_MODEL_CATEGORIES="checkpoints diffusion_models vae clip text_encoders loras controlnet gguf unet embeddings upscale_models latent_upscale_models clip_vision model_patches style_models audio_encoders diffusers configs gligen hypernetworks vae_approx frame_interpolation photomaker background_removal detection geometry_estimation optical_flow"
for dir in $COMFY_MODEL_CATEGORIES; do
    mkdir -p "$COMFYUI_DIR/models/$dir"
done

# ---------------------------------------------------------------------------
# Step 5: Install ComfyUI Manager (auto-install custom node)
# ---------------------------------------------------------------------------
echo ""
echo "=== Installing ComfyUI Manager ==="

MANAGER_DIR="$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"
checkout_git_ref "https://github.com/ltdrdata/ComfyUI-Manager.git" "$MANAGER_DIR" "$COMFYUI_MANAGER_REF"

if [ -f "$MANAGER_DIR/requirements.txt" ]; then
    "$VENV_DIR/bin/pip" install -q -r "$MANAGER_DIR/requirements.txt" -c "$PY_CONSTRAINTS"
fi

# ---------------------------------------------------------------------------
# Install OmniBridge custom nodes (local source — copied not cloned)
#
# Reinstalled on every setup run so the node code stays in lockstep with
# the gateway version (source of truth lives at
# apps/Omni_Studio/comfy_nodes/omni_bridge/).
# ---------------------------------------------------------------------------
OMNI_BRIDGE_SRC="$SOURCE_APP_DIR/comfy_nodes/omni_bridge"
OMNI_BRIDGE_DST="$COMFYUI_DIR/custom_nodes/omni_bridge"
if [ -d "$OMNI_BRIDGE_SRC" ]; then
    echo "=== Installing OmniBridge custom nodes ==="
    rm -rf "$OMNI_BRIDGE_DST"
    cp -a "$OMNI_BRIDGE_SRC" "$OMNI_BRIDGE_DST"
    echo "Installed OmniBridge -> $OMNI_BRIDGE_DST"
else
    echo "WARNING: OmniBridge source missing at $OMNI_BRIDGE_SRC"
fi

# ---------------------------------------------------------------------------
# Step 6: Set up ComfyUI-native model storage
# ---------------------------------------------------------------------------
echo ""
echo "=== Setting up model storage ==="

# ComfyUI uses its own native models tree without external model-path config.
for dir in $COMFY_MODEL_CATEGORIES; do
    mkdir -p "$COMFYUI_DIR/models/$dir"
done

# ---------------------------------------------------------------------------
# Step 7: Verify installation
# ---------------------------------------------------------------------------
echo ""
echo "=== Verifying installation ==="
if ! "$VENV_DIR/bin/python3" -c "
import torch
import transformers
import sys
print(f'PyTorch: {torch.__version__}')
print(f'Transformers: {transformers.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
torch_mm = tuple(map(int, torch.__version__.split('+')[0].split('.')[:2]))
transformers_mm = tuple(map(int, transformers.__version__.split('.')[:2]))
if torch_mm < (2, 6):
    print('ERROR: torch>=2.6 is required', file=sys.stderr)
    sys.exit(1)
if transformers_mm < (4, 45):
    print('ERROR: transformers>=4.45 is required', file=sys.stderr)
    sys.exit(1)
"; then
    echo "ERROR: PyTorch verification failed. Setup is incomplete."
    echo "The app may not work correctly. Check the log above for errors."
    exit 1
fi

# Verify ComfyUI
if [ -f "$COMFYUI_DIR/main.py" ]; then
    echo "ComfyUI: OK"
else
    echo "WARNING: ComfyUI not found at $COMFYUI_DIR"
fi

# Verify ComfyUI Manager
if [ -d "$MANAGER_DIR" ]; then
    echo "ComfyUI Manager: OK"
else
    echo "WARNING: ComfyUI Manager not installed"
fi

# Create output directories
mkdir -p "$MODELS_DIR" "$OUTPUT_DIR" "$OUTPUT_DIR/logs" "$WORKFLOWS_DIR" "$MODELS_DIR/omni" \
    "$OUTPUT_DIR/comfyui" "$OUTPUT_DIR/comfyui_input" "$CACHE_DIR/comfyui_temp" \
    "$OUTPUT_DIR/omni" "$OUTPUT_DIR/omni/tts" "$OUTPUT_DIR/omni/stt"

echo ""
echo "============================================"
echo "  Setup complete!"
echo "  ComfyUI: installed with Manager"
echo "  Omni models: install on demand via UI"
echo "============================================"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$SETUP_READY_STAMP"
