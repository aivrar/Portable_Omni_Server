import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import omni_worker
from omni_worker import (
    _INFER_TTS,
    _ensure_minicpm_dynamic_cache_compat,
    _ensure_minicpm_tts_hidden_states_compat,
    _ensure_minicpm_whisper_attention_registry,
    _hf_load_kwargs,
    _minicpm_chat_args,
    _model_input_device,
    _reject_unwired_omni_media,
    _require_cuda_vram,
    _stage_local_transformers_code,
)


def test_anygpt_uses_slow_tokenizer_to_avoid_protobuf_conversion(monkeypatch, tmp_path):
    model_dir = tmp_path / "omni" / "anygpt"
    model_dir.mkdir(parents=True)
    tokenizer_calls = []

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            tokenizer_calls.append((path, kwargs))
            return object()

    class LoadedModel:
        def eval(self):
            return self

    class FakeModel:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            return LoadedModel()

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoModelForCausalLM=FakeModel, AutoTokenizer=FakeTokenizer),
    )
    monkeypatch.setattr(omni_worker, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(omni_worker, "_variant_weights_dir", None)
    monkeypatch.setattr(omni_worker, "_precision", "fp32")

    omni_worker._load_anygpt("cpu")

    assert tokenizer_calls[0][1]["use_fast"] is False


def test_cuda_vram_guard_stops_oversized_variant_before_load(monkeypatch):
    monkeypatch.setattr(omni_worker, "_get_vram_free_mb", lambda device: 24_326)

    with pytest.raises(RuntimeError, match="requires about 60000 MB VRAM"):
        _require_cuda_vram("cuda:1", 60_000, "Qwen3-Omni 30B")

    assert _require_cuda_vram("cuda:1", 21_000, "Nemotron NVFP4") == 24_326
    assert _require_cuda_vram("cpu", 60_000, "Qwen3-Omni 30B") == 0


def test_unwired_omni_audio_video_is_explicitly_rejected(monkeypatch):
    monkeypatch.setattr(omni_worker, "_model_name", "qwen3_omni")

    with pytest.raises(omni_worker.HTTPException) as exc_info:
        _reject_unwired_omni_media({"audio": "base64", "video": "base64"})

    assert exc_info.value.status_code == 501
    assert "audio/video" in exc_info.value.detail


def test_hf_auto_placement_uses_each_logical_memory_budget(monkeypatch):
    monkeypatch.setattr(omni_worker, "_placement_config", {
        "valid": True,
        "mode": "auto",
        "hf_device_map": "auto",
        "logical_max_memory_mb": {
            "cuda:0": 22000,
            "cuda:1": 9000,
        },
        "allow_cpu": False,
    })

    kwargs = _hf_load_kwargs("dtype", "cuda:0")

    assert kwargs == {
        "torch_dtype": "dtype",
        "device_map": "auto",
        "max_memory": {0: "22000MiB", 1: "9000MiB"},
        "low_cpu_mem_usage": True,
    }


def test_sharded_input_prefers_embedding_device():
    model = SimpleNamespace(hf_device_map={
        "model.layers.0": "cuda:1",
        "model.embed_tokens": "cuda:0",
    })

    assert _model_input_device(model, "cpu") == "cuda:0"


def test_stage_local_transformers_code_copies_all_python_files(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "modeling.py").write_text("VALUE = 1\n", encoding="utf-8")
    (model_dir / "image_processing.py").write_text("VALUE = 2\n", encoding="utf-8")
    (model_dir / "weights.bin").write_bytes(b"not copied")

    target = _stage_local_transformers_code(
        model_dir,
        "minicpm_hyphen_o",
        modules_cache=tmp_path / "modules",
    )

    assert (target / "modeling.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (target / "image_processing.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (target / "weights.bin").exists()
    assert (target / "__init__.py").is_file()


def test_minicpm_whisper_compat_maps_removed_registry_to_current_class():
    class Attention:
        def forward(self, *args, **kwargs):
            return "hidden", "weights"

    module = SimpleNamespace(WhisperAttention=Attention)

    assert _ensure_minicpm_whisper_attention_registry(module) is True
    compat = module.WHISPER_ATTENTION_CLASSES["sdpa"]()
    assert isinstance(compat, Attention)
    assert compat.forward(past_key_value="cache") == (
        "hidden", "weights", "cache",
    )
    assert _ensure_minicpm_whisper_attention_registry(module) is False


def test_minicpm_dynamic_cache_compat_uses_current_sequence_length():
    class Cache:
        def get_seq_length(self):
            return 23

    module = SimpleNamespace(DynamicCache=Cache)
    assert _ensure_minicpm_dynamic_cache_compat(module) is True
    assert Cache().seen_tokens == 23
    assert _ensure_minicpm_dynamic_cache_compat(module) is False


def test_minicpm_chat_forwards_typed_generation_settings():
    messages, options = _minicpm_chat_args({
        "text": "hello",
        "max_new_tokens": 37,
        "temperature": 0.0,
        "top_p": 0.5,
    })
    assert messages == [{"role": "user", "content": ["hello"]}]
    assert options == {"max_new_tokens": 37, "sampling": False}


def test_minicpm_rejects_unwired_video_instead_of_ignoring_it():
    with pytest.raises(Exception) as error:
        _minicpm_chat_args({"text": "describe", "video": "AAAA"})
    assert getattr(error.value, "status_code", None) == 501


def test_minicpm_tts_is_registered_on_existing_worker_route():
    assert "minicpm_o" in _INFER_TTS


def test_minicpm_tts_filters_none_generation_hidden_states():
    class Model:
        def _get_last_spk_embeds(self, _inputs, outputs):
            return outputs.hidden_states

    model_obj = {"model": Model()}
    outputs = SimpleNamespace(hidden_states=(None, ("layer",), None))

    assert _ensure_minicpm_tts_hidden_states_compat(model_obj) is True
    assert model_obj["model"]._get_last_spk_embeds({}, outputs) == (("layer",),)
    assert _ensure_minicpm_tts_hidden_states_compat(model_obj) is False


def test_minicpm_tts_recovers_speaker_embedding_from_prompt_forward():
    torch = pytest.importorskip("torch")

    class FakeModel:
        def _get_last_spk_embeds(self, inputs, outputs):
            raise AssertionError("generation fallback should bypass the original helper")

        def _decode(self, inputs_embeds, tokenizer, attention_mask, **kwargs):
            return SimpleNamespace(hidden_states=(None, None))

        def llm(self, **kwargs):
            assert kwargs == {
                "input_ids": None,
                "inputs_embeds": "embeddings",
                "attention_mask": "mask",
                "output_hidden_states": True,
                "return_dict": True,
                "use_cache": False,
            }
            hidden = torch.arange(24).reshape(1, 6, 4)
            return SimpleNamespace(hidden_states=(None, hidden))

    model_obj = {"model": FakeModel()}
    outputs = SimpleNamespace(hidden_states=(None, None))
    inputs = {"spk_bounds": [[(2, 5)]]}

    assert _ensure_minicpm_tts_hidden_states_compat(model_obj) is True
    model_obj["model"]._decode("embeddings", "tokenizer", "mask")
    recovered = model_obj["model"]._get_last_spk_embeds(inputs, outputs)
    assert recovered.tolist() == [[8, 9, 10, 11], [12, 13, 14, 15], [16, 17, 18, 19]]
