"""Audio regressions with fake workers and temporary output roots."""
import asyncio
import base64
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from fastapi import HTTPException
from operation_gate import OperationGate
from worker_registry import WorkerRegistry, WorkerInfo
from routers import ace_step, audio_lab, minimax_music3, setup, outputs
import omni_outputs
import audio_lab_loaders


class AudioRepairs(unittest.TestCase):
    def test_gate_rejects_overlap_and_releases_after_cancel(self):
        async def run():
            gate = OperationGate("test")
            entered, release = asyncio.Event(), asyncio.Event()
            @gate
            async def operation():
                entered.set()
                await release.wait()
            task = asyncio.create_task(operation())
            await entered.wait()
            with self.assertRaises(HTTPException) as error:
                await operation()
            self.assertEqual(error.exception.status_code, 409)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(gate.locked())
        asyncio.run(run())

    def test_busy_workers_are_inspectable_but_not_autospawned(self):
        for module, model in ((ace_step, "ace_step"), (audio_lab, "audio_lab")):
            registry = WorkerRegistry()
            worker = WorkerInfo(worker_id="busy-1", model=model, device="cuda:0", port=8200, status="busy")
            registry.register(worker)
            ensure = getattr(module, "_ensure_" + model + "_worker")
            with patch.object(module, "worker_registry", registry), patch.object(module.worker_manager, "spawn_worker", new_callable=AsyncMock) as spawn:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(ensure(autospawn=True))
                self.assertEqual(error.exception.status_code, 409)
                self.assertIs(asyncio.run(ensure(autospawn=False, allow_busy=True)), worker)
                spawn.assert_not_awaited()

    def test_claim_does_not_overwrite_busy_owner(self):
        registry = WorkerRegistry()
        worker = WorkerInfo(worker_id="one", model="audio_lab", device="cpu", port=8200, status="ready")
        registry.register(worker)
        self.assertTrue(registry.claim_ready("one", "first"))
        self.assertFalse(registry.claim_ready("one", "second"))
        self.assertEqual(worker.current_job, "first")

    def test_ace_primary_is_not_auxiliary_membership(self):
        worker = SimpleNamespace(device="cuda:0", gpu_pool=["cuda:0", "cuda:1"])
        self.assertFalse(ace_step._ace_worker_has_device(worker, "cuda:1"))

    def test_simple_query_does_not_require_prompt(self):
        self.assertEqual(ace_step._SimpleRequest(query="Make a jazz song").prompt, "")

    def test_batch_persists_every_candidate(self):
        encoded = base64.b64encode(b"RIFF test audio").decode()
        with tempfile.TemporaryDirectory() as tmp, patch.object(ace_step, "_ACE_STEP_OUTPUT_ROOT", Path(tmp)):
            result = ace_step._save_single_result(encoded, "generate", ace_step._GenRequest(prompt="song", batch_size=2), {"audios": [{"audio_base64": encoded, "seed": 1}, {"audio_base64": encoded, "seed": 2}]})
            self.assertEqual(len(result["results"]), 2)
            self.assertEqual(len(list(Path(tmp).rglob("*.wav"))), 2)

    def test_spool_discards_inline_media_and_retains_partial_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"manifest.json").write_text('{"status":"running"}')
            candidate = audio_lab._spool_ranked_candidate(root, {"audio_base64": "UklGRg==", "seed": 3}, 1)
            self.assertNotIn("audio_base64", candidate)
            self.assertEqual(audio_lab._candidate_base64(candidate), "UklGRg==")
            audio_lab._finish_partial_manifest(root)
            self.assertEqual(json.loads((root/"manifest.json").read_text())["status"], "incomplete")

    def test_manifest_does_not_embed_source_media(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audio_lab, "_AUDIO_LAB_OUTPUT_ROOT", Path(tmp)):
            request = audio_lab._A2ARequest(prompt="sound", init_audio_base64="UklGRg==")
            result = audio_lab._save_audio_result("UklGRg==", "a2a", request)
            manifest = json.loads((Path(tmp)/result["job_id"]/"manifest.json").read_text())
            self.assertNotIn("init_audio_base64", manifest["params"])

    def test_busy_audio_prevents_asset_deletion(self):
        with patch.object(setup.worker_registry, "all_workers", return_value=[SimpleNamespace(model="audio_lab", status="ready")]):
            with self.assertRaises(HTTPException):
                setup._guard_audio_asset_delete("audio_lab")

    def test_best_effort_tempfile_creation_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(omni_outputs, "OMNI_ROOT", Path(tmp)), patch.object(omni_outputs.tempfile, "mkstemp", side_effect=OSError("disk full")):
            self.assertIsNone(omni_outputs.persist_omni_bytes(b"x", model="test", kind="tts", ext="wav"))

    def test_diffusers_rejects_ineffective_native_controls(self):
        state = SimpleNamespace(sa_format="diffusers")
        with self.assertRaises(ValueError):
            audio_lab_loaders._validate_diffusers_controls(state, {"sigma_min": 0})
        with self.assertRaises(ValueError):
            audio_lab_loaders._validate_diffusers_controls(state, {"init_noise_level": 0}, a2a=True)
        audio_lab_loaders._validate_diffusers_controls(state, {})

    def test_minimax_exact_worker_selection(self):
        registry = WorkerRegistry()
        for index in (1, 2):
            registry.register(WorkerInfo(worker_id=f"music-{index}", model="minimax_music3", device=f"cuda:{index-1}", port=8200+index, status="ready"))
        with patch.object(minimax_music3, "worker_registry", registry):
            selected = asyncio.run(minimax_music3._ensure_worker(autospawn=False, worker_id="music-2"))
            self.assertEqual(selected.worker_id, "music-2")
            with self.assertRaises(HTTPException):
                asyncio.run(minimax_music3._ensure_worker(autospawn=False))

    def test_audio_zip_contains_output(self):
        async def read(response):
            return b"".join([chunk async for chunk in response.body_iterator])
        with tempfile.TemporaryDirectory() as tmp, patch.object(audio_lab, "_AUDIO_LAB_OUTPUT_ROOT", Path(tmp)):
            job = Path(tmp)/"abcdef"
            job.mkdir()
            (job/"01.wav").write_bytes(b"RIFF")
            response = asyncio.run(audio_lab.audio_lab_zip_outputs("abcdef"))
            archive = asyncio.run(read(response))
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                self.assertEqual(zf.read("01.wav"), b"RIFF")

    def test_ranked_ace_keeps_negative_score_ahead_of_unscored_and_wraps_seed(self):
        payloads = []
        async def post(client, worker, path, payload):
            payloads.append(payload)
            return {"audio_base64": "UklGRg==", "seed": payload["seed"], "sample_rate": 48000}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ace_step, "_ACE_STEP_OUTPUT_ROOT", Path(tmp)), \
             patch.object(ace_step, "_ensure_ace_step_worker", AsyncMock(return_value=object())), \
             patch.object(ace_step, "_try_get_audio_lab_clap_worker", AsyncMock(return_value=(object(), "clap"))), \
             patch.object(ace_step, "_clear_worker_cancel", AsyncMock()), \
             patch.object(ace_step, "_check_worker_cancel", AsyncMock(return_value=False)), \
             patch.object(ace_step, "_worker_get", AsyncMock(return_value={"model_loaded": True})), \
             patch.object(ace_step, "_worker_post", post), \
             patch.object(ace_step, "_clap_score_one", AsyncMock(side_effect=[None, -0.5])):
            result = asyncio.run(ace_step.ace_step_generate_ranked(ace_step._GenRankedRequest(prompt="song", n=2, seed=2**31-1)))
            self.assertEqual([p["seed"] for p in payloads], [2**31-1, 0])
            self.assertEqual(result["results"][0]["score"], -0.5)
            self.assertTrue(result["results"][0]["best"])
            self.assertFalse(list(Path(tmp).rglob("candidate_*.wav")))

    def test_decode_rejects_oversized_header_before_read(self):
        source = Mock(samplerate=48000, channels=2, frames=48000*3600)
        context = Mock()
        context.__enter__ = Mock(return_value=source)
        context.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {"soundfile": SimpleNamespace(SoundFile=Mock(return_value=context))}):
            with self.assertRaisesRegex(ValueError, "Decoded audio"):
                audio_lab_loaders._decode_audio_base64("UklGRg==")
        source.read.assert_not_called()

    def test_ace_init_audio_modes_persist_and_preserve_precheck_error(self):
        req = ace_step._A2ARequest(prompt="song", init_audio_base64="UklGRg==")
        with tempfile.TemporaryDirectory() as tmp, patch.object(ace_step, "_ACE_STEP_OUTPUT_ROOT", Path(tmp)), patch.object(ace_step, "_ensure_ace_step_worker", AsyncMock(return_value=object())), patch.object(ace_step, "_clear_worker_cancel", AsyncMock()), patch.object(ace_step, "_worker_get", AsyncMock(return_value={"model_loaded": True})) as state, patch.object(ace_step, "_worker_post", AsyncMock(return_value={"audio_base64": "UklGRg=="})):
            result = asyncio.run(ace_step.ace_step_a2a(req))
            self.assertEqual(len(result["results"]), 1)
            state.return_value = {"model_loaded": False}
            with self.assertRaises(HTTPException) as error:
                asyncio.run(ace_step.ace_step_a2a(req))
            self.assertEqual(error.exception.status_code, 503)

    def test_audio_lab_ranked_persists_best_and_invalid_score_last(self):
        scores = iter([None, -0.25])
        async def post(client, worker, path, payload):
            if path.endswith("audio_score"):
                return {"score": next(scores)}
            return {"audio_base64": "UklGRg==", "seed": payload["seed"], "duration_s": 1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(audio_lab, "_AUDIO_LAB_OUTPUT_ROOT", Path(tmp)), patch.object(audio_lab, "_ensure_audio_lab_worker", AsyncMock(return_value=object())), patch.object(audio_lab, "_clear_worker_cancel", AsyncMock()), patch.object(audio_lab, "_check_worker_cancel", AsyncMock(return_value=False)), patch.object(audio_lab, "_worker_get", AsyncMock(return_value={"sa": {"variant_id": "sa"}, "clap": {"variant_id": "clap"}})), patch.object(audio_lab, "_worker_post", post):
            result = asyncio.run(audio_lab.audio_lab_generate_ranked(audio_lab._GenerateRankedRequest(prompt="sound", n=2, seed=1)))
            best = result["results"][0]
            self.assertEqual(best["score"], -0.25)
            self.assertTrue(best["best"])
            self.assertTrue((Path(tmp)/result["job_id"]/best["best_filename"]).is_file())
            self.assertTrue(result["results"][1]["score_failed"])

    def test_vae_only_load_rebuilds_current_model_with_retained_settings(self):
        worker = SimpleNamespace(device="cuda:0", gpu_device_map={})
        state = {"model_loaded": True, "model_variant": "ace-1.5", "vae_swap": "alternate", "model_load_kwargs": {"bf16": False, "cpu_offload": True, "int8": False, "torch_compile": False}}
        with patch.object(ace_step, "_ensure_ace_step_worker", AsyncMock(return_value=worker)), patch.object(ace_step, "_worker_get", AsyncMock(return_value=state)), patch.object(ace_step, "_worker_post", AsyncMock(return_value=state)) as post, patch.object(ace_step, "_check_load_capacity", return_value={"valid": True}):
            asyncio.run(ace_step.ace_step_load(ace_step._LoadRequest(vae_variant="default")))
            self.assertEqual(post.await_args.args[2], "/ace_step/load_model")
            self.assertEqual(post.await_args.args[3]["model_variant"], "ace-1.5")
            self.assertFalse(post.await_args.args[3]["bf16"])

    def test_compose_drains_stderr_beyond_pipe_capacity(self):
        async def run():
            return await asyncio.wait_for(outputs._run_compose_ffmpeg(
                [sys.executable, "-c", "import sys; sys.stderr.write('x'*200000)"], asyncio.Event()), 10)
        code, tail = asyncio.run(run())
        self.assertEqual(code, 0)
        self.assertEqual(len(tail), 8000)
