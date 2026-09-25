import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from omni_worker import _qwen_gptq_options


def test_qwen_gptq_adds_nested_thinker_block_hint(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "quantization_config": {
            "bits": 4,
            "group_size": 128,
            "quant_method": "gptq",
        },
    }), encoding="utf-8")

    options = _qwen_gptq_options(tmp_path, "qwen-omni-7b-gptq-int4")

    assert options["block_name_to_quantize"] == "thinker.model.layers"
    assert options["bits"] == 4


def test_qwen_base_variant_does_not_add_gptq_config(tmp_path):
    assert _qwen_gptq_options(tmp_path, "qwen-omni-7b") is None


def test_qwen_gptq_rejects_missing_quantization_config(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid GPTQ quantization config"):
        _qwen_gptq_options(tmp_path, "qwen-omni-7b-gptq-int4")
