"""Regression tests for the 2026-05-29 non-security fix pass.

Each test pins a fix from that pass so it can't silently regress. Scoped to the
pure, Windows-runnable logic (no WSL/GPU); the streaming/loader/HTTP paths still
require a live runtime to exercise fully.

  - H3    audio_lab_install / ace_step_install registered in JOB_KINDS
  - JOB-2  eviction TTL measured from finished_at, not started_at
  - H2     keep/ prune exemption (shared has_keep_segment, used by the endpoint
           and the maintenance task so they can't drift)
  - GAT-4  token_matches rejects a non-ASCII credential without raising (no 500)
  - WOR-6  parse_tool_calls handles '}' inside string values and ignores a
           non-object <tool_call> block instead of crashing
  - ROU-4  _safe_float degrades a non-numeric score instead of raising
  - BRI-3  bridge._LengthBoundedReader caps a streamed body at Content-Length
  - logs   _complete_line_bytes advances the byte cursor exactly (non-UTF-8 safe)
"""

import asyncio
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

_POSIX_SIGNALS = hasattr(os, "killpg") and hasattr(signal, "SIGKILL")

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
for _p in (str(ROOT), str(SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# bridge.py creates its log/runtime dirs at import time; point them at a temp
# dir so importing it here can't touch /opt or create stray dirs.
_BRIDGE_TMP = tempfile.mkdtemp(prefix="omni_bridge_test_")
os.environ.setdefault("OMNI_LOG_DIR", os.path.join(_BRIDGE_TMP, "log"))
os.environ.setdefault("OMNI_RUNTIME_DIR", os.path.join(_BRIDGE_TMP, "rt"))


class JobKindsTests(unittest.TestCase):
    def test_audio_and_ace_install_kinds_registered(self):
        from jobs import JOB_KINDS
        # Without these, every Audio-Lab / ACE-Step install endpoint 500s (H3).
        self.assertIn("audio_lab_install", JOB_KINDS)
        self.assertIn("ace_step_install", JOB_KINDS)
        self.assertIn("default_install", JOB_KINDS)


class EvictionTTLTests(unittest.TestCase):
    def test_ttl_measured_from_finished_at(self):
        from jobs import Job, JobStore
        store = JobStore(ttl_seconds=100, max_jobs=500)
        now = time.time()
        recent = Job(job_id="recent", kind="maintenance", status="done",
                     started_at=now - 10_000, finished_at=now - 5)
        old = Job(job_id="old", kind="maintenance", status="done",
                  started_at=now - 10_000, finished_at=now - 10_000)
        fallback = Job(job_id="fb", kind="maintenance", status="done",
                       started_at=now - 10_000, finished_at=None)
        running = Job(job_id="run", kind="maintenance", status="running",
                      started_at=now - 10_000, finished_at=None)
        store._jobs = {j.job_id: j for j in (recent, old, fallback, running)}
        store._evict()
        # A long-running job that finished 5s ago must survive a 100s TTL even
        # though it *started* 10000s ago — the whole point of JOB-2.
        self.assertIn("recent", store._jobs)
        self.assertNotIn("old", store._jobs)
        # No finished_at -> falls back to started_at (old) -> evicted.
        self.assertNotIn("fb", store._jobs)
        # Non-terminal jobs are never TTL-evicted.
        self.assertIn("run", store._jobs)


class KeepSegmentTests(unittest.TestCase):
    def test_keep_segment_detection(self):
        from output_meta import has_keep_segment
        self.assertTrue(has_keep_segment("a/keep/b.png"))
        self.assertTrue(has_keep_segment("keep/x.png"))
        self.assertTrue(has_keep_segment("x/y/keep/z/file.wav"))
        self.assertFalse(has_keep_segment("a/b.png"))
        self.assertFalse(has_keep_segment("file.png"))
        # 'keep' as the final segment (a file literally named keep) is NOT exempt.
        self.assertFalse(has_keep_segment("a/b/keep"))


class TokenMatchesTests(unittest.TestCase):
    def test_ascii_match_and_mismatch(self):
        from security import token_matches
        self.assertTrue(token_matches("abc123", "abc123"))
        self.assertFalse(token_matches("abc123", "abc124"))
        self.assertFalse(token_matches(None, "abc123"))
        self.assertFalse(token_matches("", "abc123"))

    def test_non_ascii_returns_false_not_raises(self):
        from security import token_matches
        # Starlette decodes header/query values as latin-1, so a credential can
        # carry bytes > 0x7F. token_matches must return False, never raise (GAT-4).
        self.assertFalse(token_matches("caf\xe9-token", "asciitoken1234567890"))
        self.assertFalse(token_matches("\xff\xfe", "asciitoken1234567890"))


class ParseToolCallsTests(unittest.TestCase):
    def test_single_call(self):
        from tools_compat import parse_tool_calls
        visible, calls = parse_tool_calls(
            '<tool_call>{"name": "search", "arguments": {"q": "hi"}}</tool_call>')
        self.assertEqual(visible, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "search")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"q": "hi"})

    def test_brace_inside_string_value(self):
        from tools_compat import parse_tool_calls
        visible, calls = parse_tool_calls(
            '<tool_call>{"name": "f", "arguments": {"note": "a}b"}}</tool_call>')
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"note": "a}b"})

    def test_non_object_block_ignored_not_crash(self):
        from tools_compat import parse_tool_calls
        # A <tool_call> block holding non-object JSON must be ignored, not raise
        # AttributeError on obj.get (WOR-6 hardening).
        for body in ("123", '"just text"', "true", "null"):
            _visible, calls = parse_tool_calls(f"<tool_call>{body}</tool_call>")
            self.assertEqual(calls, [], body)

    def test_multiple_calls(self):
        from tools_compat import parse_tool_calls
        visible, calls = parse_tool_calls(
            '<tool_call>{"name":"a","arguments":{}}</tool_call>'
            ' text '
            '<tool_call>{"name":"b","arguments":{"x":1}}</tool_call>')
        self.assertEqual([c["function"]["name"] for c in calls], ["a", "b"])
        self.assertEqual(visible, "text")


class SafeFloatTests(unittest.TestCase):
    def test_safe_float(self):
        from routers.audio_lab import _safe_float
        self.assertEqual(_safe_float("3.5"), 3.5)
        self.assertEqual(_safe_float(2), 2.0)
        self.assertEqual(_safe_float(None), 0.0)
        self.assertEqual(_safe_float("abc"), 0.0)
        self.assertEqual(_safe_float("abc", 1.0), 1.0)


class AudioLabStableAudioRegressionTests(unittest.TestCase):
    def test_legacy_chinese_rap_lora_is_not_advertised_as_attachable(self):
        from config import ACE_STEP_LORAS

        entry = ACE_STEP_LORAS["chinese-rap"]
        self.assertFalse(entry["available"])
        self.assertIn("adapter_config.json", entry["unavailable_reason"])

    def test_stale_audio_lab_sentinel_does_not_count_as_installed(self):
        import config

        old_root = config.AUDIO_LAB_ROOT
        old_entry = config.STABLE_AUDIO_MODELS.get("__unit_audio__")
        try:
            with tempfile.TemporaryDirectory() as td:
                config.AUDIO_LAB_ROOT = Path(td)
                config.STABLE_AUDIO_MODELS["__unit_audio__"] = {
                    "display": "Unit Audio",
                    "repo": "unit/audio",
                    "weights_dir": "unit-audio",
                    "format": "diffusers",
                    "size_gb": 1,
                    "tier": "official",
                }
                weights_dir = Path(td) / "unit-audio"
                weights_dir.mkdir()
                (weights_dir / ".install_complete").write_text("unit/audio\n", encoding="utf-8")

                self.assertFalse(config.is_audio_lab_variant_installed("__unit_audio__"))
                (weights_dir / "model.safetensors").write_bytes(b"x")
                self.assertFalse(config.is_audio_lab_variant_installed("__unit_audio__"))
                (weights_dir / "model_index.json").write_text("{}", encoding="utf-8")
                self.assertTrue(config.is_audio_lab_variant_installed("__unit_audio__"))
        finally:
            config.AUDIO_LAB_ROOT = old_root
            if old_entry is None:
                config.STABLE_AUDIO_MODELS.pop("__unit_audio__", None)
            else:
                config.STABLE_AUDIO_MODELS["__unit_audio__"] = old_entry

    def test_custom_diffusers_format_returns_nested_load_dir(self):
        from audio_lab_loaders import _detect_custom_format

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "nested" / "stable-audio"
            nested.mkdir(parents=True)
            (nested / "model_index.json").write_text("{}", encoding="utf-8")

            fmt, load_dir = _detect_custom_format(root)
            self.assertEqual(fmt, "diffusers")
            self.assertEqual(load_dir, nested)

    def test_audio_lab_sampling_payload_validation(self):
        from fastapi import HTTPException
        from routers.audio_lab import _validate_sampling_payload

        self.assertEqual(
            _validate_sampling_payload({"sampler": "dpmpp-3m-sde", "sigma_min": 0.3, "sigma_max": 500})["sampler"],
            "dpmpp-3m-sde",
        )
        with self.assertRaises(HTTPException):
            _validate_sampling_payload({"sampler": "not-a-sampler"})
        with self.assertRaises(HTTPException):
            _validate_sampling_payload({"sigma_min": 10, "sigma_max": 1})

    def test_audio_lab_sampler_families_match_diffusion_objective(self):
        from audio_lab_loaders import (
            _AudioLabState,
            _samplers_for_objective,
            _validate_native_sampler,
        )

        self.assertIn("euler", _samplers_for_objective("rf_denoiser"))
        self.assertNotIn("dpmpp-2m-sde", _samplers_for_objective("rf_denoiser"))
        self.assertIn("dpmpp-2m-sde", _samplers_for_objective("v"))
        state = _AudioLabState(sa_objective="rf_denoiser")
        _validate_native_sampler(state, "euler")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            _validate_native_sampler(state, "dpmpp-2m-sde")

    def test_tuned_vae_uses_declared_stock_profile_and_checkpoint(self):
        from audio_lab_loaders import (
            _LatentDistShim,
            _NativeVAEShim,
            _find_vae_weights,
            _stable_audio_2_0_vae_config,
        )

        cfg = _stable_audio_2_0_vae_config()
        self.assertEqual(cfg["model_type"], "autoencoder")
        self.assertEqual(cfg["sample_rate"], 44100)
        self.assertEqual(cfg["model"]["downsampling_ratio"], 2048)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            preferred = root / "sao_vae_tune_100k_unwrapped.ckpt"
            preferred.write_bytes(b"preferred")
            (root / "sao_vae_tune_100k_frozen_unwrapped.ckpt").write_bytes(b"other")
            self.assertEqual(
                _find_vae_weights("sao-vae-tuned-100k", root),
                preferred,
            )

        class _Native:
            downsampling_ratio = 2048

        self.assertEqual(_NativeVAEShim(_Native(), None).hop_length, 2048)
        marker = object()
        latent = _LatentDistShim(marker)
        self.assertIs(latent.latent_dist.sample(), marker)


class BridgeBoundedReaderTests(unittest.TestCase):
    def test_caps_at_limit_chunked(self):
        import bridge
        r = bridge._LengthBoundedReader(io.BytesIO(b"x" * 100), 30)
        out = b""
        while True:
            chunk = r.read(8)
            if not chunk:
                break
            out += chunk
        self.assertEqual(len(out), 30)          # stops at Content-Length, not EOF
        self.assertEqual(r.read(8), b"")

    def test_read_all_then_empty(self):
        import bridge
        r = bridge._LengthBoundedReader(io.BytesIO(b"abcdef"), 4)
        self.assertEqual(r.read(-1), b"abcd")   # no size -> remaining limit
        self.assertEqual(r.read(-1), b"")

    def test_limit_exceeds_available(self):
        import bridge
        r = bridge._LengthBoundedReader(io.BytesIO(b"abc"), 100)
        self.assertEqual(r.read(50), b"abc")
        self.assertEqual(r.read(50), b"")


class LogCursorTests(unittest.TestCase):
    def test_advance_is_exact_bytes_with_non_utf8(self):
        from routers.logs import _complete_line_bytes
        raw = b"abc\xffdef\nGHIJK"           # \xff is invalid UTF-8
        complete = _complete_line_bytes(raw, 64 * 1024)
        self.assertEqual(complete, b"abc\xffdef\n")
        # advance == bytes consumed (8), so the cursor stays exact even though
        # decoding \xff -> U+FFFD would have re-encoded to 3 bytes (the bug).
        self.assertEqual(len(complete), 8)

    def test_no_newline_partial_waits(self):
        from routers.logs import _complete_line_bytes
        self.assertEqual(_complete_line_bytes(b"partial line no nl", 64 * 1024), b"")

    def test_no_newline_full_buffer_flushes(self):
        from routers.logs import _complete_line_bytes
        raw = b"x" * 16
        self.assertEqual(_complete_line_bytes(raw, 16), raw)

    def test_returns_through_last_newline(self):
        from routers.logs import _complete_line_bytes
        raw = b"line1\nline2\npartial"
        self.assertEqual(_complete_line_bytes(raw, 64 * 1024), b"line1\nline2\n")


class JobStoreCancelStateMachineTests(unittest.IsolatedAsyncioTestCase):
    """TES-3/JOB-6 (cross-platform half): the cancel state machine for an
    in-process callable job — running -> cancelling -> cancelled, cancel_event
    fired, finished_at stamped, active_key released. Runs everywhere."""

    async def test_callable_cancel_transitions_to_cancelled(self):
        from jobs import JobStore
        store = JobStore()
        started = asyncio.Event()

        async def fn(progress, cancel_event):
            started.set()
            await cancel_event.wait()   # observe the cancel
            return {"ok": True}         # cancelling -> cancelled in the runner

        job = store.enqueue_callable("maintenance", fn, active_key="k1")
        await asyncio.wait_for(started.wait(), timeout=5)
        self.assertTrue(store.is_active("k1"))
        task = job.task
        self.assertEqual(store.cancel(job.job_id), "cancelling")
        self.assertTrue(job.cancel_event.is_set())
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.task)
        self.assertFalse(store.is_active("k1"))   # active_key released

    async def test_cancel_missing_and_terminal(self):
        from jobs import JobStore
        store = JobStore()
        self.assertEqual(store.cancel("does-not-exist"), "missing")

        async def fn(progress, cancel_event):
            return {"done": True}

        job = store.enqueue_callable("maintenance", fn)
        task = job.task
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(job.status, "done")
        self.assertIsNone(job.task)
        # Cancel after terminal is idempotent and returns the terminal status.
        self.assertEqual(store.cancel(job.job_id), "done")


@unittest.skipUnless(_POSIX_SIGNALS, "POSIX process-group signal cancel only")
class JobStoreSubprocessCancelTests(unittest.IsolatedAsyncioTestCase):
    """TES-3/JOB-6 (POSIX half, intent #25): subprocess cancel sends SIGTERM,
    then escalates to SIGKILL after SIGKILL_GRACE_SECONDS. The child's exit
    returncode (-SIGTERM vs -SIGKILL) proves which signal actually killed it,
    so the escalation is asserted directly rather than by timing alone."""

    @staticmethod
    def _force_kill(job):
        proc = job.process
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    @staticmethod
    async def _await(pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("condition not met within timeout")

    async def _spawn_child(self, store, *, ignore_sigterm):
        from jobs import JobStore  # noqa: F401 (import lives with the store)
        trap = ("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                if ignore_sigterm else "")
        code = (trap +
                "import sys, time\n"
                "sys.stdout.write('up\\n'); sys.stdout.flush()\n"
                "time.sleep(120)\n")
        job = store.enqueue_subprocess(
            "maintenance", [sys.executable, "-c", code], timeout=60)
        self.addCleanup(self._force_kill, job)
        # Wait until the child is actually running and (if trapping) has
        # installed its handler — it prints 'up' only after that.
        await self._await(
            lambda: job.process is not None and "up" in "".join(job.stdout_tail),
            timeout=10)
        return job

    async def test_sigterm_cancels_cooperative_child(self):
        from jobs import JobStore
        store = JobStore()
        job = await self._spawn_child(store, ignore_sigterm=False)
        task = job.task
        self.assertEqual(store.cancel(job.job_id), "cancelling")
        await asyncio.wait_for(task, timeout=10)
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.process)
        self.assertIsNone(job.task)
        self.assertEqual(job.process_returncode, -signal.SIGTERM)

    async def test_sigkill_escalation_for_sigterm_ignoring_child(self):
        from jobs import JobStore
        store = JobStore()
        job = await self._spawn_child(store, ignore_sigterm=True)
        t0 = time.monotonic()
        task = job.task
        self.assertEqual(store.cancel(job.job_id), "cancelling")
        await asyncio.wait_for(task, timeout=10)
        # The child ignores SIGTERM, so a dead state proves the SIGKILL
        # escalation fired; -SIGKILL confirms the killing signal, and the
        # elapsed time confirms it waited out the grace period first.
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(job.process)
        self.assertIsNone(job.task)
        self.assertEqual(job.process_returncode, -signal.SIGKILL)
        self.assertGreaterEqual(time.monotonic() - t0,
                                store.SIGKILL_GRACE_SECONDS - 0.5)


if __name__ == "__main__":
    unittest.main()
