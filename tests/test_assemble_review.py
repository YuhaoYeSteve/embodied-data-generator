"""Local timing tests, including a small FFmpeg round trip; no model calls."""
import hashlib
import importlib
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    ar = importlib.import_module('assemble_review')
except ModuleNotFoundError as exc:
    if exc.name != 'assemble_review': raise
    ar = None


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ar, 'assemble_review must implement an independently verified derived preview')

    def test_overhanging_audio_is_trimmed_to_the_video_sample_boundary(self):
        plan = ar.audio_plan(Fraction(721, 24), 32000, 962560)
        self.assertEqual(plan['target_samples'], 961333)
        self.assertEqual(plan['trim_tail_samples'], 1227)
        self.assertEqual(plan['pad_head_samples'], 0)
        self.assertEqual(plan['pad_tail_samples'], 0)

    def test_absent_tail_audio_is_filled_only_with_technical_silence(self):
        plan = ar.audio_plan(Fraction(30), 32000, 957440)
        self.assertEqual(plan['used_samples'], 957440)
        self.assertEqual(plan['pad_tail_samples'], 2560)
        self.assertEqual(plan['trim_head_samples'], 0)

    def test_positive_audio_offset_is_preserved_instead_of_moving_speech(self):
        plan = ar.audio_plan(Fraction(1), 32000, 32000, audio_offset_seconds=0.08)
        self.assertEqual(plan['pad_head_samples'], 2560)
        self.assertEqual(plan['trim_tail_samples'], 2560)
        self.assertEqual(plan['used_samples'], 29440)

    def test_negative_audio_offset_trims_only_samples_before_video_start(self):
        plan = ar.audio_plan(Fraction(1), 32000, 33024, audio_offset_seconds=-0.032)
        self.assertEqual(plan['trim_head_samples'], 1024)
        self.assertEqual(plan['used_samples'], 32000)
        self.assertEqual(plan['pad_tail_samples'], 0)

    def test_clock_check_detects_the_observed_concat_gap(self):
        bad = ar.timing_summary([0, 1/24, 2/24, 2/24 + 0.079997], 1/24)
        good = ar.timing_summary([0, 1/24, 2/24, 3/24], 1/24)
        self.assertFalse(bad['uniform'])
        self.assertTrue(good['uniform'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_local_round_trip_preserves_frames_audio_samples_and_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = []
            for index, (color, duration) in enumerate([('red', '0.58'), ('blue', '0.42')]):
                source = root / ('source_%d.mov' % index)
                subprocess.run(['ffmpeg', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                                'color=c=%s:size=64x64:rate=24:duration=0.5' % color,
                                '-f', 'lavfi', '-i', 'sine=frequency=%d:sample_rate=32000:duration=%s' % (440 + index*220, duration),
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
                               check=True, capture_output=True)
                sources.append(source)
            hashes = [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources]
            report = ar.assemble_preview(sources, root / 'preview')
            self.assertEqual(report['video']['source_frame_count'], 24)
            self.assertEqual(report['video']['preview_frame_count'], 24)
            self.assertTrue(report['checks']['decoded_video_frames_identical'])
            self.assertTrue(report['checks']['aligned_pcm_matches_trim_pad_only'])
            self.assertTrue(report['checks']['video_pts_uniform'])
            self.assertTrue(report['checks']['all_dts_strictly_increasing'])
            self.assertTrue(report['checks']['audio_video_lengths_aligned'])
            self.assertFalse(report['preview_warnings'])
            self.assertTrue((root / 'preview' / 'raw_concat.mov').exists())
            self.assertTrue((root / 'preview' / 'derived_preview.mov').exists())
            self.assertIn('derived', report['description'].lower())
            self.assertEqual(hashes, [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources])


if __name__ == '__main__':
    unittest.main()
