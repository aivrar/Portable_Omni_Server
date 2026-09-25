import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import ace_step_loaders  # noqa: E402
from routers.ace_step import _GenRequest, _gen_payload  # noqa: E402


class _FakeState:
    def __init__(self, variant, cfg=4.0):
        self.model_variant = variant
        self.model_default_cfg = cfg


def _model_info(variant):
    if not variant:
        return None
    turbo = "turbo" in variant or variant == "ace-1.5"
    return {
        "variant_id": variant,
        "shift_default": 3.0 if turbo else 1.0,
        "cfg_default": 1.0 if turbo else 4.0,
    }


class AceStepGenerationParamsTests(unittest.TestCase):
    def test_gateway_accepts_official_dit_metadata(self):
        req = _GenRequest(
            prompt="dark techno",
            shift=3.0,
            bpm=126,
            keyscale="D minor",
            timesignature="4",
            scheduler="dpmpp",
            negative_prompt="clipping",
            guidance_interval=[0.2, 0.8],
            autospawn=False,
        )
        payload = _gen_payload(req)
        self.assertEqual(3.0, payload["shift"])
        self.assertEqual(126, payload["bpm"])
        self.assertEqual("D minor", payload["keyscale"])
        self.assertEqual("4", payload["timesignature"])
        self.assertEqual("dpmpp", payload["scheduler"])
        self.assertEqual("clipping", payload["negative_prompt"])
        self.assertEqual([0.2, 0.8], payload["guidance_interval"])
        self.assertNotIn("autospawn", payload)

    def test_shift_out_of_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            _GenRequest(prompt="x", shift=0.5)
        with self.assertRaises(ValidationError):
            _GenRequest(prompt="x", shift=6.0)

    def test_turbo_default_shift_is_three(self):
        state = _FakeState("ace-xl-turbo", cfg=1.0)
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            self.assertEqual(3.0, ace_step_loaders._resolve_shift(state, {}))
            self.assertEqual(2.5, ace_step_loaders._resolve_shift(state, {"shift": 2.5}))

    def test_sft_default_shift_is_one(self):
        state = _FakeState("ace-sft", cfg=4.0)
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            self.assertEqual(1.0, ace_step_loaders._resolve_shift(state, {}))

    def test_v15_overrides_forward_dropped_fields(self):
        state = _FakeState("ace-xl-turbo", cfg=1.0)
        req = {
            "scheduler": "dpmpp",
            "negative_prompt": "muddy mix",
            "guidance_interval": [0.3, 0.7],
            "bpm": 128,
            "keyscale": "A minor",
            "timesignature": "4",
        }
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            overrides = ace_step_loaders._v15_request_overrides(state, req)
        self.assertEqual(3.0, overrides["shift"])
        self.assertEqual("dpmpp", overrides["sampler_mode"])
        self.assertEqual("muddy mix", overrides["lm_negative_prompt"])
        self.assertEqual(0.3, overrides["cfg_interval_start"])
        self.assertEqual(0.7, overrides["cfg_interval_end"])
        self.assertEqual(128, overrides["bpm"])
        self.assertEqual("A minor", overrides["keyscale"])
        self.assertEqual("4", overrides["timesignature"])
        self.assertEqual("ode", overrides["infer_method"])
        self.assertFalse(overrides["use_adg"])
        self.assertTrue(overrides["dcw_enabled"])

    def test_lm_defaults_to_second_visible_cuda(self):
        state = _FakeState("ace-xl-sft")
        state.device = "cuda:0"
        with patch.object(ace_step_loaders, "_visible_cuda_count", return_value=2):
            self.assertEqual(
                "cuda:1",
                ace_step_loaders._resolve_lm_runtime_device(state, None),
            )
            self.assertEqual(
                "cuda:0",
                ace_step_loaders._resolve_lm_runtime_device(state, "cuda:0"),
            )
        with patch.object(ace_step_loaders, "_visible_cuda_count", return_value=1):
            self.assertEqual(
                "cuda:0",
                ace_step_loaders._resolve_lm_runtime_device(state, None),
            )

    def test_sft_official_quality_knobs_forward(self):
        state = _FakeState("ace-xl-sft", cfg=4.0)
        req = {
            "scheduler": "euler",
            "shift": 1.0,
            "infer_method": "ode",
            "use_adg": False,
            "thinking": False,
        }
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            overrides = ace_step_loaders._v15_request_overrides(state, req)
        self.assertEqual(1.0, overrides["shift"])
        self.assertEqual("euler", overrides["sampler_mode"])
        self.assertEqual("ode", overrides["infer_method"])
        self.assertFalse(overrides["use_adg"])
        self.assertFalse(overrides["dcw_enabled"])
        self.assertEqual("en", overrides["vocal_language"])

    def test_official_helper_fields_forward(self):
        state = _FakeState("ace-xl-sft", cfg=7.0)
        req = {
            "scheduler": "euler",
            "lm_temperature": 0.85,
            "lm_cfg_scale": 2.0,
            "lm_top_k": 0,
            "lm_top_p": 0.9,
            "audio_codes": "<|audio_code_1|>",
            "dcw_mode": "double",
            "dcw_scaler": 0.05,
            "timesteps": [0.97, 0.5, 0.0],
            "fade_in_s": 0.1,
            "vocal_language": "en",
        }
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            overrides = ace_step_loaders._v15_request_overrides(state, req)
        self.assertEqual(0.85, overrides["lm_temperature"])
        self.assertEqual(2.0, overrides["lm_cfg_scale"])
        self.assertEqual("<|audio_code_1|>", overrides["audio_codes"])
        self.assertEqual("double", overrides["dcw_mode"])
        self.assertEqual([0.97, 0.5, 0.0], overrides["timesteps"])
        self.assertEqual(0.1, overrides["fade_in_duration"])
        self.assertFalse(overrides["dcw_enabled"])

    def test_velocity_and_cover_strength_forward(self):
        state = _FakeState("ace-xl-sft", cfg=7.0)
        req = {
            "scheduler": "euler",
            "velocity_norm_threshold": 1.5,
            "velocity_ema_factor": 0.2,
        }
        with patch.object(ace_step_loaders, "get_ace_step_model", side_effect=_model_info):
            overrides = ace_step_loaders._v15_request_overrides(state, req)
        self.assertEqual(1.5, overrides["velocity_norm_threshold"])
        self.assertEqual(0.2, overrides["velocity_ema_factor"])
        req = _GenRequest(
            prompt="cover this",
            audio_cover_strength=0.35,
            cover_noise_strength=0.1,
            autospawn=False,
        )
        payload = _gen_payload(req)
        self.assertEqual(0.35, payload["audio_cover_strength"])
        self.assertEqual(0.1, payload["cover_noise_strength"])

    def test_dpmpp_is_not_collapsed_to_euler(self):
        self.assertEqual("dpmpp", ace_step_loaders._resolve_sampler_mode("dpmpp"))
        self.assertEqual("heun", ace_step_loaders._resolve_sampler_mode("heun"))
        self.assertEqual("euler", ace_step_loaders._resolve_sampler_mode("unknown"))

    def test_unknown_generation_fields_are_filtered(self):
        from dataclasses import dataclass

        @dataclass
        class FakeParams:
            shift: float = 1.0
            sampler_mode: str = "euler"

        filtered = ace_step_loaders._filter_generation_kwargs(FakeParams, {
            "shift": 3.0,
            "sampler_mode": "dpmpp",
            "not_a_real_field": True,
        })
        self.assertEqual({"shift": 3.0, "sampler_mode": "dpmpp"}, filtered)


if __name__ == "__main__":
    unittest.main()
