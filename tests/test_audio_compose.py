import unittest
from pathlib import Path

from server.routers.outputs import (
    AudioComposeSegment,
    _build_audio_compose_filter,
    _relative_to_output_root,
)
from jobs import JOB_KINDS


class AudioComposeTests(unittest.TestCase):
    def test_output_path_uses_resolved_root_for_persistent_symlink(self):
        class ResolvesTo:
            def __init__(self, value):
                self.value = Path(value)

            def __fspath__(self):
                return str(self.value)

            def resolve(self):
                return self.value

        target = ResolvesTo("/var/lib/omni_studio/output/omni/compositions/id/track.wav")
        public_root = ResolvesTo("/var/lib/omni_studio/output/omni")

        self.assertEqual(
            "compositions/id/track.wav",
            _relative_to_output_root(target, public_root),
        )

    def test_audio_compose_is_registered_job_kind(self):
        self.assertIn("audio_compose", JOB_KINDS)

    def test_filter_resamples_trims_gains_and_crossfades(self):
        segments = [
            AudioComposeSegment(path="one.wav", start_s=2.0, end_s=12.0, gain_db=-1.5),
            AudioComposeSegment(path="two.wav"),
        ]

        graph, label = _build_audio_compose_filter(
            segments,
            sample_rate=48000,
            crossfade_s=4.0,
            loudness_normalize=False,
            target_lufs=-14.0,
            true_peak_db=-1.0,
        )

        self.assertIn("aresample=48000", graph)
        self.assertIn("atrim=start=2.000000:end=12.000000", graph)
        self.assertIn("volume=-1.5000dB", graph)
        self.assertIn("acrossfade=d=4.000000:c1=tri:c2=tri", graph)
        self.assertEqual("mix1", label)

    def test_filter_can_append_loudness_master(self):
        segments = [
            AudioComposeSegment(path="one.wav"),
            AudioComposeSegment(path="two.wav"),
        ]

        graph, label = _build_audio_compose_filter(
            segments,
            sample_rate=44100,
            crossfade_s=2.5,
            loudness_normalize=True,
            target_lufs=-16.0,
            true_peak_db=-1.5,
        )

        self.assertIn("loudnorm=I=-16.000:LRA=11:TP=-1.500[master]", graph)
        self.assertEqual("master", label)


if __name__ == "__main__":
    unittest.main()
