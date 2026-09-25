"""
Omni Studio Configuration

Central configuration for ComfyUI + Omni model server.
Reads paths from /opt/omni_studio/env.conf (written by setup.sh).
ComfyUI runs as managed instances; omni models use shared venv + overrides.
"""

import os
import math
from pathlib import Path

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()

_ENV_CONF = Path("/opt/omni_studio/env.conf")


def _read_env_conf() -> dict:
    """Read key=value pairs from env.conf (values may be quoted)."""
    conf = {}
    if _ENV_CONF.exists():
        for line in _ENV_CONF.read_text().strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                conf[k.strip()] = v
    return conf


_conf = _read_env_conf()

if not _ENV_CONF.exists():
    print(f"WARNING: {_ENV_CONF} not found — setup.sh has not run yet, using defaults")
elif _conf:
    _REQUIRED_CONF_KEYS = ("VENV_DIR", "MODELS_DIR", "APP_DIR")
    _missing_keys = [k for k in _REQUIRED_CONF_KEYS if k not in _conf]
    if _missing_keys:
        print(f"WARNING: env.conf missing keys: {', '.join(_missing_keys)} — using defaults")

# Directories inside the Linux filesystem (fast I/O)
APP_DIR = Path(_conf.get("APP_DIR", "/opt/omni_studio"))
SOURCE_APP_DIR = Path(_conf.get("SOURCE_APP_DIR", str(BASE_DIR.parent)))
SERVER_DIR = Path(_conf.get("SERVER_DIR", "/opt/omni_studio/server"))
VENV_DIR = Path(_conf.get("VENV_DIR", "/opt/omni_studio/venv"))
OVERRIDES_DIR = Path(_conf.get("OVERRIDES_DIR", "/opt/omni_studio/overrides"))
COMFYUI_DIR = Path(_conf.get("COMFYUI_DIR", "/opt/omni_studio/comfyui"))
COMFYUI_MODELS_DIR = COMFYUI_DIR / "models"
COMFYUI_CACHE_DIR = COMFYUI_DIR / ".cache"
CACHE_DIR = Path(_conf.get("CACHE_DIR", "/opt/omni_studio/cache"))
RUNTIME_DIR = Path(_conf.get("RUNTIME_DIR", str(CACHE_DIR / "runtime")))
PID_DIR = Path(_conf.get("PID_DIR", str(RUNTIME_DIR / "pids")))
API_TOKEN_FILE = Path(_conf.get("API_TOKEN_FILE", str(RUNTIME_DIR / "api_token")))

# Models, output, workflows, and runtime caches remain on WSL ext4.
MODELS_DIR = Path(_conf.get("MODELS_DIR", str(APP_DIR / "models")))
OUTPUT_DIR = Path(_conf.get("OUTPUT_DIR", str(APP_DIR / "output")))
WORKFLOWS_DIR = Path(_conf.get("WORKFLOWS_DIR", str(APP_DIR / "workflows")))

# Python executable inside the shared venv
PYTHON_PATH = VENV_DIR / "bin" / "python3"

# HuggingFace token storage
HF_TOKEN_FILE = Path("/opt/omni_studio/hf_token")


def _positive_int(value: str | int | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _available_cpu_count() -> int:
    """Return CPUs this process may actually use, not the WSL host total."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 2)


_cpu_total = _available_cpu_count()
OMNI_CPU_WORKERS = _positive_int(
    _conf.get("OMNI_CPU_WORKERS"),
    max(1, math.ceil(_cpu_total / 2)),
)
_download_cpu_ceiling = max(1, _cpu_total // 3)
OMNI_DOWNLOAD_WORKERS = min(
    _positive_int(_conf.get("OMNI_DOWNLOAD_WORKERS"), _download_cpu_ceiling),
    _download_cpu_ceiling,
)
OMNI_MAX_CONCURRENT_DOWNLOADS = _positive_int(
    _conf.get("OMNI_MAX_CONCURRENT_DOWNLOADS"),
    3,
)


def setup_environment():
    """Configure environment variables for HuggingFace, CUDA, etc."""
    for path in (
        MODELS_DIR, MODELS_DIR / "hub", MODELS_DIR / "xet", MODELS_DIR / "torch",
        MODELS_DIR / "hf_datasets",
        MODELS_DIR / "lora", MODELS_DIR / "omni", COMFYUI_MODELS_DIR,
        COMFYUI_CACHE_DIR / "huggingface" / "hub",
        COMFYUI_CACHE_DIR / "huggingface" / "xet",
        OUTPUT_DIR, WORKER_LOG_DIR, WORKFLOWS_DIR,
        CACHE_DIR / "pip", CACHE_DIR / "xdg", CACHE_DIR / "tmp",
        CACHE_DIR / "pycache", CACHE_DIR / "comfyui_temp", CACHE_DIR / "thumbs",
        CACHE_DIR / "torch_extensions", CACHE_DIR / "triton", CACHE_DIR / "cuda",
        CACHE_DIR / "numba", CACHE_DIR / "matplotlib", RUNTIME_DIR, PID_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(MODELS_DIR)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(MODELS_DIR / "hub")
    os.environ["HF_XET_CACHE"] = str(MODELS_DIR / "xet")
    os.environ["TORCH_HOME"] = str(MODELS_DIR / "torch")
    os.environ["TRANSFORMERS_CACHE"] = str(MODELS_DIR / "hub")
    os.environ["PIP_CACHE_DIR"] = str(CACHE_DIR / "pip")
    os.environ["PIP_PROGRESS_BAR"] = "off"
    os.environ["XDG_CACHE_HOME"] = str(CACHE_DIR / "xdg")
    os.environ["TMPDIR"] = str(CACHE_DIR / "tmp")
    os.environ["PYTHONPYCACHEPREFIX"] = str(CACHE_DIR / "pycache")
    os.environ["TORCH_EXTENSIONS_DIR"] = str(CACHE_DIR / "torch_extensions")
    os.environ["TRITON_CACHE_DIR"] = str(CACHE_DIR / "triton")
    os.environ["CUDA_CACHE_PATH"] = str(CACHE_DIR / "cuda")
    os.environ["NUMBA_CACHE_DIR"] = str(CACHE_DIR / "numba")
    os.environ["MPLCONFIGDIR"] = str(CACHE_DIR / "matplotlib")
    os.environ["HF_DATASETS_CACHE"] = str(MODELS_DIR / "hf_datasets")
    os.environ["IMAGEIO_FFMPEG_EXE"] = "/usr/bin/ffmpeg"
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["MAX_JOBS"] = str(OMNI_CPU_WORKERS)
    os.environ["CMAKE_BUILD_PARALLEL_LEVEL"] = str(OMNI_CPU_WORKERS)
    os.environ["MAKEFLAGS"] = f"-j{OMNI_CPU_WORKERS}"
    os.environ["NINJAFLAGS"] = f"-j{OMNI_CPU_WORKERS}"
    os.environ["OMP_NUM_THREADS"] = str(OMNI_CPU_WORKERS)
    os.environ["OPENBLAS_NUM_THREADS"] = str(OMNI_CPU_WORKERS)
    os.environ["MKL_NUM_THREADS"] = str(OMNI_CPU_WORKERS)
    os.environ["NUMEXPR_NUM_THREADS"] = str(OMNI_CPU_WORKERS)
    os.environ["HF_XET_NUM_CONCURRENT_RANGE_GETS"] = str(OMNI_DOWNLOAD_WORKERS)
    os.environ["OMNI_CPU_WORKERS"] = str(OMNI_CPU_WORKERS)
    os.environ["OMNI_DOWNLOAD_WORKERS"] = str(OMNI_DOWNLOAD_WORKERS)
    os.environ["OMNI_MAX_CONCURRENT_DOWNLOADS"] = str(OMNI_MAX_CONCURRENT_DOWNLOADS)
    os.environ["OMNI_APP_INSTANCE"] = str(APP_DIR)


# ---------------------------------------------------------------------------
# API Server
# ---------------------------------------------------------------------------
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8200

# ---------------------------------------------------------------------------
# ComfyUI instance management
# ---------------------------------------------------------------------------
COMFYUI_DEFAULT_REF = "1ac60da2c9c8f83654204b2a1db13908cf7614f7"
COMFYUI_PINNED_REF = str(
    os.environ.get("OMNI_COMFYUI_REF")
    or _conf.get("OMNI_COMFYUI_REF")
    or COMFYUI_DEFAULT_REF
).strip()
COMFYUI_PORT_MIN = 8188
COMFYUI_PORT_MAX = 8199
COMFYUI_MAX_INSTANCES = 8
# Current ComfyUI releases can spend several minutes importing PyTorch,
# frontend packages, Manager nodes, and filesystem metadata after a large
# model run evicts the WSL page cache. Keep the wait bounded and configurable,
# but do not kill an otherwise healthy cold start at the old four-minute mark.
COMFYUI_STARTUP_TIMEOUT = min(900, max(60, _positive_int(
    os.environ.get("OMNI_COMFYUI_STARTUP_TIMEOUT")
    or _conf.get("OMNI_COMFYUI_STARTUP_TIMEOUT"),
    600,
)))
COMFYUI_HEALTH_INTERVAL = 10   # seconds
COMFYUI_MAX_HEALTH_FAILURES = 3

# VRAM modes for ComfyUI
COMFYUI_VRAM_MODES = {
    "normal": [],
    "force_normal": ["--normalvram"],
    "high": ["--highvram"],
    "gpu_only": ["--gpu-only"],
    "low": ["--lowvram"],
    "none": ["--novram"],
    "cpu": ["--cpu"],
}

# ---------------------------------------------------------------------------
# Omni model worker management
# ---------------------------------------------------------------------------
WORKER_PORT_MIN = 8201
WORKER_PORT_MAX = 8250
WORKER_HEALTH_INTERVAL = 10
WORKER_STARTUP_TIMEOUT = 180  # omni models can be large
WORKER_MAX_HEALTH_FAILURES = 3
WORKER_LOG_DIR = OUTPUT_DIR / "logs"

# Request bounds used by the gateway and model workers.
INFER_MAX_TEXT_CHARS = 32768
INFER_MAX_IMAGE_BASE64_CHARS = 20 * 1024 * 1024
INFER_MAX_IMAGE_BYTES = 15 * 1024 * 1024
INFER_MAX_IMAGE_PIXELS = 25_000_000
INFER_MAX_NEW_TOKENS = 4096
INFER_MAX_TEMPERATURE = 2.0
INFER_MAX_TOP_P = 1.0

_worker_default_device: str | None = None


def _detect_default_device() -> str:
    global _worker_default_device
    if _worker_default_device is not None:
        return _worker_default_device
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        if r.returncode == 0:
            _worker_default_device = "cuda:0"
            return _worker_default_device
    except Exception:
        pass
    _worker_default_device = "cpu"
    return _worker_default_device


class _LazyDevice(str):
    """String subclass that defers nvidia-smi detection to first access."""
    def __new__(cls):
        return str.__new__(cls, "")
    def __str__(self):
        return _detect_default_device()
    def __repr__(self):
        return repr(_detect_default_device())
    def __eq__(self, other):
        return _detect_default_device() == other
    def __ne__(self, other):
        return _detect_default_device() != other
    def __hash__(self):
        return hash(_detect_default_device())
    def __bool__(self):
        return bool(_detect_default_device())
    def startswith(self, prefix, *args):
        return _detect_default_device().startswith(prefix, *args)
    def split(self, *args, **kwargs):
        return _detect_default_device().split(*args, **kwargs)
    def __contains__(self, item):
        return item in _detect_default_device()
    def __add__(self, other):
        return _detect_default_device() + other
    def __radd__(self, other):
        return other + _detect_default_device()
    def __format__(self, format_spec):
        return format(_detect_default_device(), format_spec)


WORKER_DEFAULT_DEVICE = _LazyDevice()

# ---------------------------------------------------------------------------
# Omni model -> override mapping
# None = uses base venv only, string = override directory name
# ---------------------------------------------------------------------------
MODEL_OVERRIDE_MAP = {
    "qwen_omni_3b": "qwen",
    "qwen_omni_7b": "qwen",
    "minicpm_o": "minicpm",
    "moshi": "moshi",
    "anygpt": "anygpt",
    "minimax_music3": "minimax_music3",
}

MOSS_TTS_MODEL_ID = "moss_tts"
MOSS_SFX_MODEL_ID = "moss_sfx"
MOSS_REPO_DIR = OVERRIDES_DIR / "moss_tts_repo"
MOSS_TTS_VENV_DIR = OVERRIDES_DIR / "moss_tts_venv"
MOSS_SFX_VENV_DIR = OVERRIDES_DIR / "moss_sfx_venv"
MOSS_TTS_WEIGHTS_DIR = "moss-tts-local-v1.5"
MOSS_TTS_CODEC_WEIGHTS_DIR = "moss-audio-tokenizer-v2"
MOSS_SFX_WEIGHTS_DIR = "moss-soundeffect-v2.0"

# ---------------------------------------------------------------------------
# Omni model metadata (for UI display and installation)
# ---------------------------------------------------------------------------
OMNI_MODEL_SETUP = {
    "qwen_omni_3b": {
        "display": "Qwen2.5-Omni-3B",
        "desc": "Omni-modal 3B -- text, image, audio, video",
        "weights_repo": "Qwen/Qwen2.5-Omni-3B",
        "weights_dir": "qwen-omni-3b",
        "weights_size": "~8GB",
        "vram": "~8GB (fp16)",
        "override": "qwen",
    },
    "qwen_omni_7b": {
        "display": "Qwen2.5-Omni-7B",
        "desc": "Omni-modal 7B -- text, image, audio, video",
        "weights_repo": "Qwen/Qwen2.5-Omni-7B",
        "weights_dir": "qwen-omni-7b",
        "weights_size": "~14GB",
        "vram": "~23GB (fp16), ~12GB (int4)",
        "override": "qwen",
    },
    "minicpm_o": {
        "display": "MiniCPM-o 2.6",
        "desc": "Edge omni-modal ~8B -- text, image, audio, TTS output",
        "weights_repo": "openbmb/MiniCPM-o-2_6",
        "weights_dir": "minicpm-o",
        "weights_size": "~16GB",
        "vram": "~18GB (fp16), ~8GB (int4)",
        "override": "minicpm",
    },
    "moshi": {
        "display": "Moshi 7B",
        "desc": "Real-time full-duplex voice conversation (200ms latency)",
        "weights_repo": "kyutai/moshiko-pytorch-bf16",
        "weights_dir": "moshi",
        "weights_size": "~14GB",
        "vram": "~16GB (bf16)",
        "override": "moshi",
    },
    "anygpt": {
        "display": "AnyGPT 7B",
        "desc": "Any-to-any multimodal -- text, speech, music, image",
        "weights_repo": "fnlp/AnyGPT-chat",
        "weights_dir": "anygpt",
        "weights_size": "~14GB",
        "vram": "~16GB (fp16)",
        "override": "anygpt",
    },
    MOSS_TTS_MODEL_ID: {
        "display": "MOSS-TTS Local v1.5",
        "desc": "48 kHz stereo TTS with direct speech generation and voice cloning support",
        "weights_repo": "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5",
        "weights_dir": MOSS_TTS_WEIGHTS_DIR,
        "weights_size": "~10GB plus codec",
        "vram": "~12-16GB (bf16)",
        "override": "moss_tts_venv",
        "default_install": False,
    },
    MOSS_SFX_MODEL_ID: {
        "display": "MOSS-SoundEffect v2.0",
        "desc": "48 kHz text-to-sound-effects DiT generator",
        "weights_repo": "OpenMOSS-Team/MOSS-SoundEffect-v2.0",
        "weights_dir": MOSS_SFX_WEIGHTS_DIR,
        "weights_size": "~8GB",
        "vram": "~10-12GB (bf16)",
        "override": "moss_sfx_venv",
        "default_install": False,
    },
    "qwen3_omni": {
        "display": "Qwen3-Omni 30B (MoE)",
        "desc": "Native end-to-end omni -- text, image, audio, video; Thinker-Talker MoE",
        "weights_repo": "Qwen/Qwen3-Omni-30B-A3B-Instruct",
        "weights_dir": "qwen3-omni-30b-instruct",
        "weights_size": "~60GB",
        "vram": "~60GB (fp16), ~32GB (fp8)",
        "override": "qwen3",
    },
    "nemotron_nano_omni": {
        "display": "Nemotron 3 Nano Omni 30B (MoE)",
        "desc": "NVIDIA omni-modal -- video, audio, image, text; long-context agent reasoning",
        "weights_repo": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16",
        "weights_dir": "nemotron-nano-omni-30b-bf16",
        "weights_size": "~62GB",
        "vram": "~62GB (bf16), ~33GB (fp8), ~21GB (nvfp4)",
        "override": "nemotron",
    },
}

# ---------------------------------------------------------------------------
# Model variants (quantizations, sizes, voice variants) per model family.
# Each model_id in OMNI_MODEL_SETUP has a list of variants. The 'default'
# variant corresponds to the original OMNI_MODEL_SETUP entry -- this is the
# backward-compatible entry. New variants can be downloaded and selected
# when spawning a worker.
# ---------------------------------------------------------------------------
OMNI_MODEL_VARIANTS = {
    "qwen_omni_3b": [
        {
            "variant_id": "base",
            "display": "Qwen2.5-Omni-3B (fp16, base)",
            "repo": "Qwen/Qwen2.5-Omni-3B",
            "weights_dir": "qwen-omni-3b",
            "size": "~8GB",
            "vram": "~8GB",
            "quant": None,
            "default": True,
        },
    ],
    "qwen_omni_7b": [
        {
            "variant_id": "base",
            "display": "Qwen2.5-Omni-7B (fp16, base)",
            "repo": "Qwen/Qwen2.5-Omni-7B",
            "weights_dir": "qwen-omni-7b",
            "size": "~14GB",
            "vram": "~23GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "awq",
            "display": "Qwen2.5-Omni-7B AWQ (4-bit)",
            "repo": "Qwen/Qwen2.5-Omni-7B-AWQ",
            "weights_dir": "qwen-omni-7b-awq",
            "size": "~4GB",
            "vram": "~6GB",
            "quant": "awq",
            "default": False,
        },
        {
            "variant_id": "gptq-int4",
            "display": "Qwen2.5-Omni-7B GPTQ-Int4",
            "repo": "Qwen/Qwen2.5-Omni-7B-GPTQ-Int4",
            "weights_dir": "qwen-omni-7b-gptq-int4",
            "size": "~4GB",
            "vram": "~6GB",
            "quant": "gptq",
            "default": False,
        },
    ],
    "minicpm_o": [
        {
            "variant_id": "2_6",
            "display": "MiniCPM-o 2.6 (fp16, base)",
            "repo": "openbmb/MiniCPM-o-2_6",
            "weights_dir": "minicpm-o",
            "size": "~16GB",
            "vram": "~18GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "2_6-int4",
            "display": "MiniCPM-o 2.6 int4",
            "repo": "openbmb/MiniCPM-o-2_6-int4",
            "weights_dir": "minicpm-o-2_6-int4",
            "size": "~8GB",
            "vram": "~10GB",
            "quant": "int4",
            "default": False,
        },
        {
            "variant_id": "4_5",
            "display": "MiniCPM-o 4.5 (9B, fp16)",
            "repo": "openbmb/MiniCPM-o-4_5",
            "weights_dir": "minicpm-o-4_5",
            "size": "~18GB",
            "vram": "~20GB",
            "quant": None,
            "default": False,
        },
        {
            "variant_id": "4_5-int4",
            "display": "MiniCPM-o 4.5 int4",
            "repo": "openbmb/MiniCPM-o-4_5-int4",
            "weights_dir": "minicpm-o-4_5-int4",
            "size": "~8GB",
            "vram": "~10GB",
            "quant": "int4",
            "default": False,
        },
    ],
    "moshi": [
        {
            "variant_id": "moshiko-bf16",
            "display": "Moshiko (male voice, bf16)",
            "repo": "kyutai/moshiko-pytorch-bf16",
            "weights_dir": "moshi",
            "size": "~14GB",
            "vram": "~16GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "moshika-bf16",
            "display": "Moshika (female voice, bf16)",
            "repo": "kyutai/moshika-pytorch-bf16",
            "weights_dir": "moshi-moshika-bf16",
            "size": "~14GB",
            "vram": "~16GB",
            "quant": None,
            "default": False,
        },
        {
            "variant_id": "moshiko-q8",
            "display": "Moshiko (male voice, int8)",
            "repo": "kyutai/moshiko-pytorch-q8",
            "weights_dir": "moshi-moshiko-q8",
            "size": "~7GB",
            "vram": "~9GB",
            "quant": "int8",
            "default": False,
        },
        {
            "variant_id": "moshika-q8",
            "display": "Moshika (female voice, int8)",
            "repo": "kyutai/moshika-pytorch-q8",
            "weights_dir": "moshi-moshika-q8",
            "size": "~7GB",
            "vram": "~9GB",
            "quant": "int8",
            "default": False,
        },
    ],
    "anygpt": [
        {
            "variant_id": "chat",
            "display": "AnyGPT-chat (instruction-tuned)",
            "repo": "fnlp/AnyGPT-chat",
            "weights_dir": "anygpt",
            "size": "~14GB",
            "vram": "~16GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "base",
            "display": "AnyGPT-base",
            "repo": "fnlp/AnyGPT-base",
            "weights_dir": "anygpt-base",
            "size": "~14GB",
            "vram": "~16GB",
            "quant": None,
            "default": False,
        },
    ],
    MOSS_TTS_MODEL_ID: [
        {
            "variant_id": "local-v1.5",
            "display": "MOSS-TTS Local Transformer v1.5",
            "repo": "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5",
            "weights_dir": MOSS_TTS_WEIGHTS_DIR,
            "size": "~10GB plus codec",
            "vram": "~12-16GB",
            "quant": None,
            "default": True,
            "paired_codec_repo": "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2",
            "paired_codec_dir": MOSS_TTS_CODEC_WEIGHTS_DIR,
        },
    ],
    MOSS_SFX_MODEL_ID: [
        {
            "variant_id": "sfx-v2.0",
            "display": "MOSS-SoundEffect v2.0",
            "repo": "OpenMOSS-Team/MOSS-SoundEffect-v2.0",
            "weights_dir": MOSS_SFX_WEIGHTS_DIR,
            "size": "~8GB",
            "vram": "~10-12GB",
            "quant": None,
            "default": True,
        },
    ],
    "qwen3_omni": [
        {
            "variant_id": "instruct",
            "display": "Qwen3-Omni 30B Instruct",
            "repo": "Qwen/Qwen3-Omni-30B-A3B-Instruct",
            "weights_dir": "qwen3-omni-30b-instruct",
            "size": "~60GB",
            "vram": "~60GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "thinking",
            "display": "Qwen3-Omni 30B Thinking",
            "repo": "Qwen/Qwen3-Omni-30B-A3B-Thinking",
            "weights_dir": "qwen3-omni-30b-thinking",
            "size": "~60GB",
            "vram": "~60GB",
            "quant": None,
            "default": False,
        },
        {
            "variant_id": "captioner",
            "display": "Qwen3-Omni 30B Captioner",
            "repo": "Qwen/Qwen3-Omni-30B-A3B-Captioner",
            "weights_dir": "qwen3-omni-30b-captioner",
            "size": "~60GB",
            "vram": "~60GB",
            "quant": None,
            "default": False,
        },
    ],
    "nemotron_nano_omni": [
        {
            "variant_id": "bf16",
            "display": "Nemotron 3 Nano Omni 30B Reasoning (bf16)",
            "repo": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16",
            "weights_dir": "nemotron-nano-omni-30b-bf16",
            "size": "~62GB",
            "vram": "~62GB",
            "quant": None,
            "default": True,
        },
        {
            "variant_id": "fp8",
            "display": "Nemotron 3 Nano Omni 30B Reasoning (fp8)",
            "repo": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
            "weights_dir": "nemotron-nano-omni-30b-fp8",
            "size": "~33GB",
            "vram": "~33GB",
            "quant": "fp8",
            "default": False,
        },
        {
            "variant_id": "nvfp4",
            "display": "Nemotron 3 Nano Omni 30B Reasoning (nvfp4)",
            "repo": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4",
            "weights_dir": "nemotron-nano-omni-30b-nvfp4",
            "size": "~21GB",
            "vram": "~21GB",
            "quant": "nvfp4",
            "default": False,
        },
    ],
}

# Model families that support PEFT/LoRA adapters (standard HF transformers)
LORA_COMPATIBLE_MODELS = {"qwen_omni_3b", "qwen_omni_7b", "minicpm_o", "qwen3_omni"}

# LoRA storage directory inside the WSL runtime model tree
LORA_DIR = MODELS_DIR / "lora"


def get_variant(model_id: str, variant_id: str | None = None) -> dict | None:
    """Return the matching variant dict, or the default variant if variant_id is None."""
    variants = OMNI_MODEL_VARIANTS.get(model_id, [])
    if not variants:
        return None
    if variant_id is None:
        for v in variants:
            if v.get("default"):
                return v
        return variants[0]
    for v in variants:
        if v["variant_id"] == variant_id:
            return v
    return None


# Per-model inference timeouts (seconds)
MODEL_INFER_TIMEOUT = {
    "qwen_omni_3b": 600.0,
    "qwen_omni_7b": 900.0,
    "minicpm_o": 600.0,
    "moshi": 300.0,
    "anygpt": 600.0,
    "qwen3_omni": 1200.0,         # 30B MoE, longer load + audio/video processing
    "nemotron_nano_omni": 1200.0, # 30B MoE
    MOSS_TTS_MODEL_ID: 1200.0,
    MOSS_SFX_MODEL_ID: 900.0,
}
DEFAULT_INFER_TIMEOUT = 300.0

# ---------------------------------------------------------------------------
# ComfyUI model categories
# ---------------------------------------------------------------------------
COMFYUI_MODEL_CATEGORIES = [
    "checkpoints", "diffusion_models", "vae", "clip", "text_encoders",
    "loras", "controlnet", "gguf", "unet", "embeddings",
    "upscale_models", "latent_upscale_models", "clip_vision",
    "model_patches", "style_models", "audio_encoders", "clip_projections", "diffusers",
    "configs", "gligen", "hypernetworks", "vae_approx",
    "frame_interpolation", "photomaker", "background_removal",
    "detection", "geometry_estimation", "optical_flow",
]

# Asset upload caps (multipart uploads to /api/assets/comfy/upload/*)
try:
    OMNI_ASSET_UPLOAD_MAX_GB = max(1, int(os.environ.get("OMNI_ASSET_UPLOAD_MAX_GB", "30")))
except (TypeError, ValueError):
    OMNI_ASSET_UPLOAD_MAX_GB = 30
OMNI_ASSET_UPLOAD_MAX_BYTES = OMNI_ASSET_UPLOAD_MAX_GB * 1024 * 1024 * 1024

ASSET_WEIGHT_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")

# ---------------------------------------------------------------------------
# OpenAI compatibility shim
# ---------------------------------------------------------------------------
# Map OpenAI-style model names to native Omni model IDs. Override via env JSON.
import json as _json

_DEFAULT_OPENAI_ALIASES = {
    "gpt-3.5-turbo": "qwen_omni_3b",
    "gpt-3.5-turbo-instruct": "qwen_omni_3b",
    "gpt-4": "qwen_omni_7b",
    "gpt-4-turbo": "qwen_omni_7b",
    "gpt-4o": "qwen_omni_7b",
    "gpt-4o-mini": "qwen_omni_3b",
    "tts-1": MOSS_TTS_MODEL_ID,
    "moss-tts": MOSS_TTS_MODEL_ID,
    "moss-sfx": MOSS_SFX_MODEL_ID,
}
_alias_env = os.environ.get("OMNI_OPENAI_ALIASES", "").strip()
if _alias_env:
    try:
        _parsed = _json.loads(_alias_env)
        if isinstance(_parsed, dict):
            OMNI_OPENAI_ALIASES = {str(k): str(v) for k, v in _parsed.items()}
        else:
            OMNI_OPENAI_ALIASES = dict(_DEFAULT_OPENAI_ALIASES)
    except _json.JSONDecodeError:
        OMNI_OPENAI_ALIASES = dict(_DEFAULT_OPENAI_ALIASES)
else:
    OMNI_OPENAI_ALIASES = dict(_DEFAULT_OPENAI_ALIASES)


# ===========================================================================
# Audio Lab registries
# ---------------------------------------------------------------------------
# Stable Audio (text→audio, audio→audio, inpainting, VAE-only, unconditional)
# via stable-audio-tools (native) and diffusers.StableAudioPipeline. CLAP
# scoring via transformers.ClapModel. A single audio_lab worker family holds
# one active SA model + one CLAP model + optional swapped VAE.
# ===========================================================================

AUDIO_LAB_MODEL_ID = "audio_lab"

# Stable Audio variants. `format` selects the loader at runtime:
#   "diffusers" → diffusers.StableAudioPipeline.from_pretrained(local_dir)
#   "native"    → stable_audio_tools.models.factory.create_model_from_config(...)
# `tier` is "official" | "community" | "untested" — surfaced as a UI badge.
# `weights_dir` is relative to AUDIO_LAB_ROOT (defined below).
STABLE_AUDIO_MODELS = {
    # — Tier 1: official Stability releases (verified, supported) —
    "sao-open-1.0":           {"display": "Stable Audio Open 1.0",                       "repo": "stabilityai/stable-audio-open-1.0",          "weights_dir": "sao-open-1.0",           "format": "diffusers", "size_gb": 5, "vram_gb": 6, "max_duration_s": 47, "gated": True,  "default": True, "tier": "official",  "tags": ["general"]},
    "sao-open-small":         {"display": "Stable Audio Open Small (Arm)",               "repo": "stabilityai/stable-audio-open-small",        "weights_dir": "sao-open-small",         "format": "native",    "size_gb": 2, "vram_gb": 3, "max_duration_s": 11, "gated": True, "quickstart": True,   "tier": "official",  "tags": ["small", "fast"]},
    # — Tier 2: established community fine-tunes, standard format —
    "foundation-1-diffusers": {"display": "Foundation-1 (diffusers port)",               "repo": "tintwotin/Foundation-1-Diffusers",           "weights_dir": "foundation-1-diffusers", "format": "diffusers", "size_gb": 5, "vram_gb": 6, "max_duration_s": 47,                                  "tier": "community", "tags": ["general", "quality"]},
    "foundation-1":           {"display": "Foundation-1 (RoyalCities native)",           "repo": "RoyalCities/Foundation-1",                   "weights_dir": "foundation-1",           "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["general", "quality"]},
    "rc-infinite-pianos":     {"display": "RC Infinite Pianos",                          "repo": "RoyalCities/RC_Infinite_Pianos",             "weights_dir": "rc-infinite-pianos",     "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["piano"]},
    "rc-vocal-textures":      {"display": "RC Vocal Textures",                           "repo": "RoyalCities/Vocal_Textures_Main",            "weights_dir": "rc-vocal-textures",      "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["vocal"]},
    "sao-instrumental":       {"display": "SAO Instrumental (santifiorino)",             "repo": "santifiorino/SAO-Instrumental-Finetune",     "weights_dir": "sao-instrumental",       "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["instrumental"]},
    "audialab-edm":           {"display": "Audialab EDM Elements",                       "repo": "adlb/Audialab_EDM_Elements",                 "weights_dir": "audialab-edm",           "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["edm"]},
    "nightdefined-violins":   {"display": "SAO Violins",                                 "repo": "nightdefined/stable-audio-open-violins",     "weights_dir": "nightdefined-violins",   "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["violin"]},
    "nekochu-music":          {"display": "SAO Music (Nekochu)",                         "repo": "Nekochu/stable-audio-open-1.0-Music",        "weights_dir": "nekochu-music",          "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "community", "tags": ["music"]},
    "sa1-fp8":                {"display": "SA1 fp8 quant",                               "repo": "convertor/sa1-fp8",                          "weights_dir": "sa1-fp8",                "format": "native",    "size_gb": 3, "vram_gb": 4,                                                       "tier": "community", "tags": ["fp8", "quant"]},
    # — Tier 3: untested community uploads (install OK; load may fail, badged) —
    "bharatverse-beats":      {"display": "BeatGeneration",                              "repo": "bharatverse11/BeatGeneration",               "weights_dir": "bharatverse-beats",      "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "untested",  "tags": ["beats"]},
    "kurbloid-burn":          {"display": "SAO burn (kurbloid)",                         "repo": "kurbloid/stable-audio-open-1.0-burn",        "weights_dir": "kurbloid-burn",          "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "untested",  "tags": []},
    "gluten-v1":              {"display": "Gluten v1",                                   "repo": "atoof/gluten_v1",                            "weights_dir": "gluten-v1",              "format": "native",    "size_gb": 5, "vram_gb": 6,                                                       "tier": "untested",  "tags": []},
    "innermost47-obsidian":   {"display": "Obsidian neural",                             "repo": "innermost47/obsidian-neural-models",         "weights_dir": "innermost47-obsidian",   "format": "native",    "size_gb": 6, "vram_gb": 6,                                                       "tier": "untested",  "tags": []},
    "fundwotsai-control":     {"display": "Text-to-Music Control",                       "repo": "fundwotsai2001/Text-to-Music_control_family", "weights_dir": "fundwotsai-control",     "format": "native",    "size_gb": 6, "vram_gb": 6,                                                       "tier": "untested",  "tags": ["control"]},
    "aestudio-foundation1":   {"display": "AEStudio Foundation-1 collection",            "repo": "AEmotionStudio/foundation1-models",          "weights_dir": "aestudio-foundation1",   "format": "native",    "size_gb": 6, "vram_gb": 6,                                                       "tier": "untested",  "tags": ["collection"]},
    "aestudio-sao":           {"display": "AEStudio SAO collection",                     "repo": "AEmotionStudio/stable-audio-open-models",    "weights_dir": "aestudio-sao",           "format": "native",    "size_gb": 6, "vram_gb": 6,                                                       "tier": "untested",  "tags": ["collection"]},
}

# Optional VAE (autoencoder) swap. When loaded with a non-default vae_variant,
# the worker replaces pipe.vae (diffusers) or model.pretransform (native).
STABLE_AUDIO_VAES = {
    "default":            {"display": "Default (model's own VAE)", "repo": None,                          "weights_dir": None,                     "size_gb": 0, "default": True},
    "sao-vae-tuned-100k": {
        "display": "Tuned-100k VAE (lyraaaa)",
        "repo": "lyraaaa/sao_vae_tuned_100k",
        "weights_dir": "vae/sao-vae-tuned-100k",
        "size_gb": 1,
        # The repository deliberately omits model_config.json. Its README
        # declares the stock stable_audio_2_0_vae architecture and provides
        # two raw autoencoder checkpoints; use the fully tuned one by default.
        "config_profile": "stable_audio_2_0_vae",
        "weights_file": "sao_vae_tune_100k_unwrapped.ckpt",
    },
}

# CLAP scoring models. Used by /infer/audio_score and generate-ranked.
CLAP_MODELS = {
    "larger-clap-general":  {"display": "LAION larger CLAP general",      "repo": "laion/larger_clap_general",          "weights_dir": "clap/larger_clap_general",          "size_gb": 2, "default": True},
    "clap-htsat-fused":     {"display": "CLAP HTSAT fused",               "repo": "laion/clap-htsat-fused",             "weights_dir": "clap/clap-htsat-fused",             "size_gb": 2},
    "clap-htsat-unfused":   {"display": "CLAP HTSAT unfused",             "repo": "laion/clap-htsat-unfused",           "weights_dir": "clap/clap-htsat-unfused",           "size_gb": 2},
    "larger-clap-music":    {"display": "LAION larger CLAP music+speech", "repo": "laion/larger_clap_music_and_speech", "weights_dir": "clap/larger_clap_music_and_speech", "size_gb": 2},
}

# Samplers surfaced to the UI/CLI. ``stable-audio-tools`` dispatches to a
# different sampler implementation based on the loaded model's diffusion
# objective, so keep the compatible families explicit instead of presenting
# one misleading flat list.
AUDIO_LAB_V_SAMPLERS = (
    "dpmpp-3m-sde", "dpmpp-2m-sde", "dpmpp-2m", "k-heun", "k-lms",
    "k-dpmpp-2s-ancestral", "k-dpm-2", "k-dpm-fast", "k-dpm-adaptive",
    "v-ddim", "v-ddim-cfgpp",
)
AUDIO_LAB_RF_SAMPLERS = ("euler", "rk4", "dpmpp", "pingpong")
AUDIO_LAB_SAMPLERS = AUDIO_LAB_V_SAMPLERS + AUDIO_LAB_RF_SAMPLERS

# Register audio_lab as a single worker family in the existing omni model maps
# so spawn_worker, _resolve_busy_worker, and the worker registry accept it.
OMNI_MODEL_SETUP[AUDIO_LAB_MODEL_ID] = {
    "display": "Audio Lab (Stable Audio + CLAP)",
    "desc": "Text→audio, audio→audio, inpainting, VAE-only, CLAP scoring",
    "weights_repo": None,  # per-variant; see STABLE_AUDIO_MODELS
    "weights_dir": "audio_lab",
    "weights_size": "2–6GB per variant",
    "vram": "~6–8GB combined SA+CLAP",
    "override": None,      # shared venv only
}
OMNI_MODEL_VARIANTS[AUDIO_LAB_MODEL_ID] = [
    {
        "variant_id": _v_id,
        "display":    _v["display"],
        "repo":       _v["repo"],
        "weights_dir": _v["weights_dir"],
        "size":       f"~{_v['size_gb']}GB",
        "vram":       f"~{_v.get('vram_gb', 6)}GB",
        "quant":      "fp8" if "fp8" in _v.get("tags", []) else None,
        "default":    _v.get("default", False),
        "format":     _v["format"],
        "tier":       _v["tier"],
        "tags":       _v.get("tags", []),
    }
    for _v_id, _v in STABLE_AUDIO_MODELS.items()
]
MODEL_INFER_TIMEOUT[AUDIO_LAB_MODEL_ID] = 600.0

# Filesystem locations (under MODELS_DIR, inside the WSL ext4).
AUDIO_LAB_ROOT = MODELS_DIR / "audio_lab"
AUDIO_LAB_OUTPUT_KIND = "audio_lab"  # for persist_omni_bytes(kind=...)
# Audio payloads (up to 120 s stereo float32 ≈ 42 MB base64). Used by both
# gateway and worker request schemas so caps don't diverge.
AUDIO_LAB_MAX_BASE64_CHARS = 64 * 1024 * 1024


def get_audio_lab_model(variant_id: str | None) -> dict | None:
    """Return a STABLE_AUDIO_MODELS entry merged with its variant_id.
    variant_id=None resolves to the default variant."""
    if variant_id is None:
        for _id, _v in STABLE_AUDIO_MODELS.items():
            if _v.get("default"):
                return {"variant_id": _id, **_v}
        return None
    v = STABLE_AUDIO_MODELS.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def get_audio_lab_vae(variant_id: str | None) -> dict | None:
    if variant_id in (None, "default"):
        return {"variant_id": "default", **STABLE_AUDIO_VAES["default"]}
    v = STABLE_AUDIO_VAES.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def get_clap_model(variant_id: str | None) -> dict | None:
    if variant_id is None:
        for _id, _v in CLAP_MODELS.items():
            if _v.get("default"):
                return {"variant_id": _id, **_v}
        return None
    v = CLAP_MODELS.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def audio_lab_weights_path(variant_id: str):
    """Local directory for an installed Stable Audio variant, or None if unknown."""
    v = STABLE_AUDIO_MODELS.get(variant_id)
    return (AUDIO_LAB_ROOT / v["weights_dir"]) if v else None


def audio_lab_vae_path(variant_id: str):
    if variant_id == "default":
        return None
    v = STABLE_AUDIO_VAES.get(variant_id)
    if not v or not v.get("weights_dir"):
        return None
    return AUDIO_LAB_ROOT / v["weights_dir"]


def clap_weights_path(variant_id: str):
    v = CLAP_MODELS.get(variant_id)
    return (AUDIO_LAB_ROOT / v["weights_dir"]) if v else None


_AUDIO_LAB_WEIGHT_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")


def _has_install_complete(p) -> bool:
    """Look for the sentinel that audio_lab_snapshot_download writes."""
    if p is None:
        return False
    try:
        return (p / ".install_complete").exists()
    except OSError:
        return False


def _has_weights_file(p) -> bool:
    if not p or not p.exists():
        return False
    return any(
        f.suffix.lower() in _AUDIO_LAB_WEIGHT_EXTS
        for f in p.rglob("*") if f.is_file()
    )


def is_audio_lab_variant_installed(variant_id: str) -> bool:
    """A variant is installed when weights and its loader sentinel exist.

    The installer still writes .install_complete for provenance, but a stale
    sentinel alone is not enough. Empty/partial folders must not appear in load
    dropdowns as usable weights.
    """
    p = audio_lab_weights_path(variant_id)
    if not _has_weights_file(p):
        return False
    info = STABLE_AUDIO_MODELS.get(variant_id) or {}
    try:
        if info.get("format") == "diffusers":
            return (p / "model_index.json").exists() or any(p.rglob("model_index.json"))
        if info.get("format") == "native":
            return (p / "model_config.json").exists() or any(p.rglob("model_config.json"))
    except OSError:
        return False
    return False


def is_audio_lab_vae_installed(variant_id: str) -> bool:
    if variant_id == "default":
        return True
    p = audio_lab_vae_path(variant_id)
    return _has_weights_file(p)


def is_clap_installed(variant_id: str) -> bool:
    p = clap_weights_path(variant_id)
    return _has_weights_file(p)


# ===========================================================================
# ACE-Step registries
# ---------------------------------------------------------------------------
# ACE-Step 1.5 song-generation DiT (StepFun + ACE-Studio). One ace_step worker
# family holds one DiT + one 5Hz Qwen3 LM + optional swapped VAE + LoRA stack.
# CLAP scoring for generate-ranked reuses the audio_lab worker over the
# loopback HTTP path — no duplicated CLAP weights in this worker.
# ===========================================================================

ACE_STEP_MODEL_ID = "ace_step"

# Base DiT checkpoints. `format` selects the loader path:
#   "native"    -> acestep.handler.AceStepHandler.initialize_service
#   "diffusers" → diffusers.DiffusionPipeline.from_pretrained(..., trust_remote_code=True)
# `default_lm` is the recommended 5Hz LM pairing; user may override.
# `weights_dir` is relative to ACE_STEP_ROOT (defined below).
ACE_STEP_MODELS = {
    # — Tier 1: XL family (DiT ≈4B params, ≈19GB bf16) —
    "ace-xl-turbo":            {"display": "ACE-Step v1.5 XL Turbo (8-step, no CFG)", "repo": "ACE-Step/acestep-v15-xl-turbo",            "weights_dir": "models/ace-xl-turbo",            "size_gb": 19, "vram_gb": 12, "default_lm": "ace-lm-1.7b", "steps_default": 8,  "cfg_default": 1.0, "shift_default": 3.0, "format": "native",    "tier": "official",  "default": True, "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint"]},
    "ace-xl-sft":              {"display": "ACE-Step v1.5 XL SFT",                    "repo": "ACE-Step/acestep-v15-xl-sft",              "weights_dir": "models/ace-xl-sft",              "size_gb": 19, "vram_gb": 20, "default_lm": "ace-lm-1.7b", "steps_default": 50, "cfg_default": 7.0, "shift_default": 1.0, "format": "native",    "tier": "official",                   "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint"]},
    "ace-xl-base":             {"display": "ACE-Step v1.5 XL Base (pre-train)",       "repo": "ACE-Step/acestep-v15-xl-base",             "weights_dir": "models/ace-xl-base",             "size_gb": 19, "vram_gb": 20, "default_lm": "ace-lm-1.7b", "steps_default": 50, "cfg_default": 7.0, "shift_default": 1.0, "format": "native",    "tier": "official",                   "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint", "extract", "lego", "complete"]},
    "ace-xl-turbo-diffusers":  {"display": "ACE-Step XL Turbo (diffusers port)",      "repo": "ACE-Step/acestep-v15-xl-turbo-diffusers",  "weights_dir": "models/ace-xl-turbo-diffusers",  "size_gb": 19, "vram_gb": 12, "default_lm": None,          "steps_default": 8,  "cfg_default": 1.0, "shift_default": 3.0, "format": "diffusers", "tier": "untested",                   "max_duration_s": 600, "supported_tasks": ["text2music"]},
    # — Tier 2: 2B family (DiT ≈2B params, ≈5GB bf16) —
    "ace-1.5":                 {"display": "ACE-Step v1.5 Core Bundle (2B turbo + shared VAE/text encoder)", "repo": "ACE-Step/Ace-Step1.5", "weights_dir": "models/ace-1.5", "size_gb": 12, "vram_gb": 6, "default_lm": "ace-lm-1.7b", "steps_default": 8, "cfg_default": 1.0, "shift_default": 3.0, "format": "native", "tier": "official", "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint"]},
    "ace-base":                {"display": "ACE-Step v1.5 Base (2B pre-train)",       "repo": "ACE-Step/acestep-v15-base",                "weights_dir": "models/ace-base",                "size_gb": 5,  "vram_gb": 6,  "default_lm": "ace-lm-0.6b", "steps_default": 50, "cfg_default": 7.0, "shift_default": 1.0, "format": "native",    "tier": "official",                   "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint", "extract", "lego", "complete"]},
    "ace-sft":                 {"display": "ACE-Step v1.5 SFT (2B)",                  "repo": "ACE-Step/acestep-v15-sft",                 "weights_dir": "models/ace-sft",                 "size_gb": 5,  "vram_gb": 6,  "default_lm": "ace-lm-0.6b", "steps_default": 50, "cfg_default": 7.0, "shift_default": 1.0, "format": "native",    "tier": "official",                   "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint"]},
    "ace-turbo-cont":          {"display": "ACE-Step v1.5 Turbo Continuous (2B)",     "repo": "ACE-Step/acestep-v15-turbo-continuous",    "weights_dir": "models/ace-turbo-cont",          "size_gb": 5,  "vram_gb": 6,  "default_lm": "ace-lm-0.6b", "steps_default": 8,  "cfg_default": 1.0, "shift_default": 3.0, "format": "native",    "tier": "official",                   "max_duration_s": 600, "supported_tasks": ["text2music", "cover", "repaint"]},
}

# 5Hz Qwen3-based planners. Optional for DiT-only generation but required for
# Chain-of-Thought prompt/metadata/audio-code planning.
ACE_STEP_LMS = {
    "ace-lm-0.6b": {"display": "ACE 5Hz LM 0.6B (Qwen3)", "repo": "ACE-Step/acestep-5Hz-lm-0.6B", "weights_dir": "lms/ace-lm-0.6b", "size_gb": 2, "vram_gb": 2, "default": True},
    "ace-lm-1.7b": {"display": "ACE 5Hz LM 1.7B (Qwen3)", "repo": "ACE-Step/acestep-5Hz-lm-1.7B", "weights_dir": "lms/ace-lm-1.7b", "size_gb": 4, "vram_gb": 4},
    "ace-lm-4b":   {"display": "ACE 5Hz LM 4B (Qwen3)",   "repo": "ACE-Step/acestep-5Hz-lm-4B",   "weights_dir": "lms/ace-lm-4b",   "size_gb": 9, "vram_gb": 9},
}

# 1D autoencoder swap. "default" uses the shared official VAE from the v1.5
# root/core bundle unless the user selects a replacement.
# Community VAEs (ScragVAE, HOT-Step) are well-liked drop-ins per the v1.5 model
# index but vary in state_dict layout — wrap loads in try/except.
ACE_STEP_VAES = {
    "default":           {"display": "Shared official 1D VAE (from ACE-Step v1.5 core bundle)", "repo": None,                              "weights_dir": None,                    "size_gb": 0,  "default": True},
    "ace-1d-sa-format":  {"display": "ACE 1D VAE (Stable-Audio-format export)",   "repo": "ACE-Step/ace-step-v1.5-1d-vae-stable-audio-format", "weights_dir": "vaes/ace-1d-sa-format", "size_gb": 1, "tier": "official"},
    "scrag-vae":         {"display": "ScragVAE 0.2B (community)",                 "repo": "scragnog/Ace-Step-1.5-ScragVAE",                    "weights_dir": "vaes/scrag-vae",        "size_gb": 1, "tier": "community"},
    "hot-step-cpp":      {"display": "HOT-Step CPP-PP VAE (community)",           "repo": "scragnog/HOT-Step-CPP-PP-VAE",                      "weights_dir": "vaes/hot-step-cpp",     "size_gb": 1, "tier": "community"},
}

# LoRAs (PEFT-style adapter weights). Two of the officials enable distinct
# inference modes (lyric2vocal, text2samples) — when those modes are invoked
# the worker auto-attaches the LoRA on entry and detaches on exit.
ACE_STEP_LORAS = {
    # — Official, mode-enabling —
    "lyric2vocal":      {"display": "Lyric2Vocal (unreleased upstream)",            "repo": None, "weights_dir": "loras/lyric2vocal",  "size_gb": 1, "tier": "planned", "enables_mode": "lyric2vocal",  "available": False, "unavailable_reason": "ACE-Step describes Lyric2Vocal but has not released its LoRA weights."},
    "text2samples":     {"display": "Text2Samples (unreleased upstream)",           "repo": None, "weights_dir": "loras/text2samples", "size_gb": 1, "tier": "planned", "enables_mode": "text2samples", "available": False, "unavailable_reason": "ACE-Step describes Text2Samples but has not released its LoRA weights."},
    # — Official, style —
    "chinese-rap":      {"display": "Chinese Rap (legacy incompatible package)",  "repo": "ACE-Step/ACE-Step-v1-chinese-rap-LoRA",        "weights_dir": "loras/chinese-rap",      "size_gb": 1, "tier": "official", "available": False, "unavailable_reason": "The legacy v1 repository ships config.json plus pytorch_lora_weights.safetensors but no supported PEFT adapter_config.json or LoKr artifact, so the current ACE worker cannot attach it."},
    "chinese-new-year": {"display": "Chinese New Year (official)",                 "repo": "ACE-Step/ACE-Step-v1.5-chinese-new-year-LoRA", "weights_dir": "loras/chinese-new-year", "size_gb": 1, "tier": "official"},
    # — Curated community —
    "synthpop":         {"display": "Synthpop (incompatible package metadata)",    "repo": "daydreamlive/synthpop",                        "weights_dir": "loras/synthpop",         "size_gb": 1, "tier": "community", "available": False, "unavailable_reason": "The repository has PEFT-style weights but no adapter_config.json or LoRA alpha metadata, so Omni cannot attach it without guessing model-critical parameters."},
    "lofi":             {"display": "Lofi (smoki9999)",                            "repo": "smoki9999/smoki-lofi-acestep1.5",              "weights_dir": "loras/lofi",             "size_gb": 1, "tier": "community"},
    "raga":             {"display": "Indian Raga (veeceey)",                       "repo": "veeceey/RagaLoRA-indian-music-ace-step",       "weights_dir": "loras/raga",             "size_gb": 1, "tier": "community"},
    "pop-electro":      {"display": "Pop/Electro on XL (Nekochu)",                 "repo": "Nekochu/ACE-Step-xl-base-pop-electro-lora",    "weights_dir": "loras/pop-electro",      "size_gb": 1, "tier": "community"},
    "acoustic-guitar":  {"display": "Acoustic Guitar (DisturbingTheField)",        "repo": "DisturbingTheField/ACE-Step-v1.5-acoustic-guitar-and-a-merge-LoRA",   "weights_dir": "loras/acoustic-guitar",  "size_gb": 1, "tier": "community", "adapter_files": [
        {"id": "fingerstyle", "filename": "adapter_model.safetensors", "display": "Fingerstyle acoustic guitar", "default": True, "recommended_multiplier": [0.2, 0.7]},
        {"id": "vocal-instrument-merge", "filename": "vocal_instrument_merge_adapter_model.safetensors", "display": "Raspy vocal + instrumental + acoustic guitar merge", "recommended_multiplier": [0.2, 0.7]},
    ]},
    "raspy-vocal-pack": {"display": "Raspy Vocal & Instrumental (5-LoRA pack)",    "repo": "DisturbingTheField/ACE-Step-v1.5-raspy-vocal-and-instrumental-5-LoRAs", "weights_dir": "loras/raspy-vocal-pack", "size_gb": 2, "tier": "community", "adapter_files": [
        {"id": "balanced", "filename": "adapter_model.safetensors", "display": "Vocal 0.8 + instrumental 0.8 merge", "default": True, "recommended_multiplier": [0.2, 0.7]},
        {"id": "male-vocals", "filename": "male_vocals_adapter_model.safetensors", "display": "Raspy male vocals", "recommended_multiplier": [0.2, 0.7]},
        {"id": "instrumental", "filename": "instrumental_adapter_model.safetensors", "display": "Rock / guitar / bass / drums / piano / synth instruments", "recommended_multiplier": [0.2, 0.7]},
        {"id": "instrumental-heavy", "filename": "voc_06_inst_14___adapter_model.safetensors", "display": "Vocal 0.6 + instrumental 1.4 merge", "recommended_multiplier": [0.2, 0.7]},
        {"id": "vocal-heavy", "filename": "voc_14_inst_06___adapter_model.safetensors", "display": "Vocal 1.4 + instrumental 0.6 merge", "recommended_multiplier": [0.2, 0.7]},
    ]},
}

# UI/CLI dropdown enums.
ACE_STEP_SCHEDULERS = ("euler", "heun", "dpmpp")
ACE_STEP_EDIT_MODES = ("only_lyrics", "remix")
ACE_STEP_EXTEND_MODES = ("prepend", "append")

# Register ace_step as a single worker family in the omni model maps so
# spawn_worker, _resolve_busy_worker, and the worker registry accept it.
OMNI_MODEL_SETUP[ACE_STEP_MODEL_ID] = {
    "display": "ACE Step (1.5 song generation)",
    "desc": "DiT-based 48 kHz stereo song generation with text, cover/remix, repaint, LoRA, and base-model completion modes",
    "weights_repo": None,  # per-variant; see ACE_STEP_MODELS
    "weights_dir": "ace_step",
    "weights_size": "5–19GB per checkpoint + 2–9GB LM",
    "vram": "~6GB (2B+small LM) to ~20GB (XL native)",
    "override": None,      # shared venv only
}
OMNI_MODEL_VARIANTS[ACE_STEP_MODEL_ID] = [
    {
        "variant_id":  _v_id,
        "display":     _v["display"],
        "repo":        _v["repo"],
        "weights_dir": _v["weights_dir"],
        "size":        f"~{_v['size_gb']}GB",
        "vram":        f"~{_v.get('vram_gb', 6)}GB",
        "quant":       None,
        "default":     _v.get("default", False),
        "format":      _v["format"],
        "tier":        _v.get("tier", "official"),
        "default_lm":  _v.get("default_lm"),
        "supported_tasks": _v.get("supported_tasks", []),
    }
    for _v_id, _v in ACE_STEP_MODELS.items()
]
# Song-length jobs (XL @ 50 steps for 5 min ≈ 6–10 min on a 3090). Give wide headroom.
MODEL_INFER_TIMEOUT[ACE_STEP_MODEL_ID] = 1800.0

# Filesystem layout (under MODELS_DIR, inside the WSL ext4):
#   ace_step/models/<variant>/   DiT checkpoints
#   ace_step/lms/<variant>/      5Hz LMs
#   ace_step/vaes/<variant>/     swapped VAEs
#   ace_step/loras/<name>/       LoRA weights
#   ace_step/custom/<kind>/<n>/  user-installed HF repos
ACE_STEP_ROOT = MODELS_DIR / "ace_step"
ACE_STEP_OUTPUT_KIND = "ace_step"
ACE_STEP_UPSTREAM_LM_DIRS = {
    "ace-lm-0.6b": "acestep-5Hz-lm-0.6B",
    "ace-lm-1.7b": "acestep-5Hz-lm-1.7B",
    "ace-lm-4b": "acestep-5Hz-lm-4B",
}
ACE_STEP_CORE_COMPONENTS = {
    "Qwen3-Embedding-0.6B": "Shared Qwen3 text encoder",
    "vae": "Shared official 1D VAE",
}
# Up to ~5 min of 48 kHz stereo 16-bit WAV ≈ 58 MB → base64 ≈ 77 MB. Songs up
# to 10 min stretch to ~154 MB. Cap at 256 MB for headroom + future formats.
ACE_STEP_MAX_BASE64_CHARS = 256 * 1024 * 1024


def get_ace_step_model(variant_id: str | None) -> dict | None:
    """Return an ACE_STEP_MODELS entry merged with its variant_id.
    variant_id=None resolves to the default variant."""
    if variant_id is None:
        for _id, _v in ACE_STEP_MODELS.items():
            if _v.get("default"):
                return {"variant_id": _id, **_v}
        return None
    v = ACE_STEP_MODELS.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def get_ace_step_lm(variant_id: str | None) -> dict | None:
    if variant_id is None:
        for _id, _v in ACE_STEP_LMS.items():
            if _v.get("default"):
                return {"variant_id": _id, **_v}
        return None
    v = ACE_STEP_LMS.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def get_ace_step_vae(variant_id: str | None) -> dict | None:
    if variant_id in (None, "default"):
        return {"variant_id": "default", **ACE_STEP_VAES["default"]}
    v = ACE_STEP_VAES.get(variant_id)
    return {"variant_id": variant_id, **v} if v else None


def get_ace_step_lora(name: str | None) -> dict | None:
    if not name:
        return None
    v = ACE_STEP_LORAS.get(name)
    return {"name": name, **v} if v else None


def ace_step_model_path(variant_id: str):
    v = ACE_STEP_MODELS.get(variant_id)
    return (ACE_STEP_ROOT / v["weights_dir"]) if v else None


def ace_step_lm_path(variant_id: str):
    v = ACE_STEP_LMS.get(variant_id)
    return (ACE_STEP_ROOT / v["weights_dir"]) if v else None


def ace_step_vae_path(variant_id: str):
    if variant_id == "default":
        return None
    v = ACE_STEP_VAES.get(variant_id)
    if not v or not v.get("weights_dir"):
        return None
    return ACE_STEP_ROOT / v["weights_dir"]


def ace_step_lora_path(name: str):
    v = ACE_STEP_LORAS.get(name)
    return (ACE_STEP_ROOT / v["weights_dir"]) if v else None


def _has_install_or_weights(p) -> bool:
    return _has_install_complete(p) or _has_weights_file(p)


def ace_step_upstream_lm_dir(variant_id: str) -> str | None:
    return ACE_STEP_UPSTREAM_LM_DIRS.get(variant_id)


def ace_step_lm_available_path(variant_id: str):
    """Return an installed LM path, including LMs bundled in the v1.5 root repo.

    This intentionally differs from ace_step_lm_path(), which is the managed
    standalone install/delete location.
    """
    primary = ace_step_lm_path(variant_id)
    if _has_install_or_weights(primary):
        return primary
    upstream = ace_step_upstream_lm_dir(variant_id)
    if not upstream:
        return None
    candidates = [
        ACE_STEP_ROOT / "checkpoints" / upstream,
        ACE_STEP_ROOT / "models" / "ace-1.5" / upstream,
        ACE_STEP_ROOT / "core" / upstream,
    ]
    for cand in candidates:
        if _has_install_or_weights(cand):
            return cand
    return None


def ace_step_core_component_candidates(name: str) -> list[Path]:
    return [
        ACE_STEP_ROOT / "checkpoints" / name,
        ACE_STEP_ROOT / "models" / "ace-1.5" / name,
        ACE_STEP_ROOT / "core" / name,
    ]


def ace_step_core_component_path(name: str):
    for cand in ace_step_core_component_candidates(name):
        if _has_install_or_weights(cand):
            return cand
    return None


def ace_step_core_status() -> dict:
    components = []
    missing = []
    for name, display in ACE_STEP_CORE_COMPONENTS.items():
        path = ace_step_core_component_path(name)
        installed = path is not None
        if not installed:
            missing.append(name)
        components.append({
            "name": name,
            "display": display,
            "installed": installed,
            "path": str(path) if path else None,
        })
    return {
        "core": components,
        "core_ready": not missing,
        "core_missing": missing,
    }


def is_ace_step_model_installed(variant_id: str) -> bool:
    p = ace_step_model_path(variant_id)
    if _has_install_complete(p):
        return True
    return _has_weights_file(p)


def is_ace_step_lm_installed(variant_id: str) -> bool:
    return ace_step_lm_available_path(variant_id) is not None


def is_ace_step_vae_installed(variant_id: str) -> bool:
    if variant_id == "default":
        return True
    p = ace_step_vae_path(variant_id)
    if _has_install_complete(p):
        return True
    return _has_weights_file(p)


def is_ace_step_lora_installed(name: str) -> bool:
    p = ace_step_lora_path(name)
    if _has_install_complete(p):
        return True
    return _has_weights_file(p)


# ===========================================================================
# MiniMax Music 3
# ---------------------------------------------------------------------------
# Standalone lyrics + caption -> song pipeline.  The managed Diffusers snapshot
# deliberately excludes the duplicate SGLang checkpoint layout in the official
# repository, reducing the local install from about 53.4 GiB to about 28 GiB.
# ===========================================================================

MINIMAX_MUSIC3_MODEL_ID = "minimax_music3"
MINIMAX_MUSIC3_ROOT = MODELS_DIR / "minimax_music3"
MINIMAX_MUSIC3_OUTPUT_KIND = "minimax_music3"
MINIMAX_MUSIC3_REVISION = "bd348f9c49ea3c1b39f33ace3436f8fad435f24e"
MINIMAX_MUSIC3_MAX_DURATION_S = 360.0  # 9,000 frames at 25 Hz
MINIMAX_MUSIC3_MAX_TEXT_CHARS = 20_000  # conservative proxy for 5,000-token source limit
MINIMAX_MUSIC3_MAX_TOKENS = 5_000
MINIMAX_MUSIC3_SAMPLE_RATE = 44_100    # native Diffusers output

MINIMAX_MUSIC3_MODELS = {
    "official-diffusers": {
        "display": "MiniMax Music 3 (official Diffusers)",
        "repo": "MiniMaxAI/MiniMax-Music3",
        "revision": MINIMAX_MUSIC3_REVISION,
        "weights_dir": "models/official-diffusers",
        "size_gb": 28,
        "vram_gb": 22,
        "format": "diffusers-modular",
        "tier": "official",
        "default": True,
        "sample_rate": MINIMAX_MUSIC3_SAMPLE_RATE,
        "max_duration_s": MINIMAX_MUSIC3_MAX_DURATION_S,
        "license": "MiniMax-Music3 Community License",
    },
}

MINIMAX_MUSIC3_ALLOW_PATTERNS = (
    "LICENSE", "README.md", "config.json", "modular_model_index.json",
    "tokenizer/*", "language_model/*", "rvq_depth_decoder/*",
    "condition_encoder/*", "transformer/*", "scheduler/*", "vocoder/*",
)

OMNI_MODEL_SETUP[MINIMAX_MUSIC3_MODEL_ID] = {
    "display": "MiniMax Music 3",
    "desc": "Long-form lyrics + structured-caption song generation with expressive vocals and native 44.1 kHz stereo output",
    "weights_repo": "MiniMaxAI/MiniMax-Music3",
    "weights_dir": "minimax_music3",
    "weights_size": "~28GB Diffusers subset (~53.4GB full dual-runtime repo)",
    "vram": "~22GB with automatic CPU offload; upstream also supports slower group offload",
    "override": "minimax_music3",
}
OMNI_MODEL_VARIANTS[MINIMAX_MUSIC3_MODEL_ID] = [
    {
        "variant_id": variant_id,
        "display": meta["display"],
        "repo": meta["repo"],
        "revision": meta["revision"],
        "weights_dir": meta["weights_dir"],
        "size": f"~{meta['size_gb']}GB",
        "vram": f"~{meta['vram_gb']}GB",
        "quant": None,
        "default": meta.get("default", False),
        "format": meta["format"],
        "tier": meta["tier"],
    }
    for variant_id, meta in MINIMAX_MUSIC3_MODELS.items()
]
MODEL_INFER_TIMEOUT[MINIMAX_MUSIC3_MODEL_ID] = 7200.0


def minimax_music3_model_path(variant_id: str) -> Path:
    meta = MINIMAX_MUSIC3_MODELS.get(variant_id)
    relative = meta["weights_dir"] if meta else f"models/{variant_id}"
    return MINIMAX_MUSIC3_ROOT / relative


def is_minimax_music3_model_installed(variant_id: str) -> bool:
    path = minimax_music3_model_path(variant_id)
    required = (
        "modular_model_index.json",
        "language_model/model.safetensors.index.json",
        "transformer/diffusion_pytorch_model.safetensors.index.json",
        "rvq_depth_decoder/diffusion_pytorch_model.safetensors",
        "vocoder/diffusion_pytorch_model.safetensors",
    )
    return _has_install_complete(path) and all((path / name).is_file() for name in required)


def is_minimax_music3_runtime_installed() -> bool:
    return (OVERRIDES_DIR / "minimax_music3" / ".install_complete").is_file()
