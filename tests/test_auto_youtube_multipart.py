from __future__ import annotations

from types import SimpleNamespace
import subprocess
import unittest

from vod_dashboard import auto_youtube_multipart as multipart


class MultipartPlanningTests(unittest.TestCase):
    def test_constants_and_duration_and_size_counts(self):
        self.assertEqual((multipart.HARD_DURATION_SECONDS, multipart.HARD_SIZE_BYTES), (43200, 256000000000))
        cases = [(11 * 3600 + 59 * 60, 1), (43200, 2), (12 * 3600 + 600, 2), (15 * 3600, 2), (22 * 3600, 2), (24 * 3600, 3), (30 * 3600, 3)]
        for duration, expected in cases:
            with self.subTest(duration=duration): self.assertEqual(multipart.plan_multipart_upload(duration_seconds=duration, size_bytes=1).part_count, expected)
        for size, expected in ((200_000_000_000, 1), (256_000_000_000, 2), (300_000_000_000, 2), (500_000_000_001, 3)):
            with self.subTest(size=size): self.assertEqual(multipart.plan_multipart_upload(duration_seconds=5 * 3600, size_bytes=size).part_count, expected)

    def test_split_points_are_deterministic_and_equally_spaced_to_millis(self):
        plan = multipart.plan_multipart_upload(duration_seconds=54000, size_bytes=1)
        self.assertEqual(plan.split_points_seconds, (27000.0,))
        again = multipart.plan_multipart_upload(duration_seconds=86400, size_bytes=1)
        self.assertEqual(again.split_points_seconds, (28800.0, 57600.0))
        self.assertTrue(all(a < b for a, b in zip(again.split_points_seconds, again.split_points_seconds[1:])))

    def test_limits_distinguish_original_and_generated(self):
        self.assertTrue(multipart.original_part_within_limits(duration_seconds=11 * 3600 + 59 * 60, size_bytes=200_000_000_000))
        self.assertFalse(multipart.original_part_within_limits(duration_seconds=43200, size_bytes=1))
        self.assertTrue(multipart.generated_part_within_limits(duration_seconds=42900, size_bytes=250_000_000_000))
        self.assertFalse(multipart.generated_part_within_limits(duration_seconds=42901, size_bytes=1))
        self.assertFalse(multipart.generated_part_within_limits(duration_seconds=1, size_bytes=250_000_000_001))
        self.assertFalse(multipart.generated_part_within_limits(duration_seconds=1, size_bytes=256_000_000_000))


class MultipartProbeTests(unittest.TestCase):
    def payload(self, duration="12.5"):
        return {"format": {"duration": duration}, "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "duration": "12.5"}, {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2, "duration": "12.5"}]}

    def test_duration_parse_and_stream_signature(self):
        result = multipart.parse_ffprobe_payload(self.payload())
        self.assertEqual(result.duration_seconds, 12.5)
        self.assertTrue(multipart.stream_signatures_match(result.streams, result.streams))
        self.assertFalse(multipart.stream_signatures_match(result.streams, (multipart.StreamDescriptor("video", "hevc", 1920, 1080),)))
        self.assertFalse(multipart.stream_signatures_match(result.streams, (result.streams[0],)))
        self.assertFalse(multipart.stream_signatures_match(result.streams, (multipart.StreamDescriptor("video", "h264", 1280, 1080), result.streams[1])))
        self.assertFalse(multipart.stream_signatures_match(result.streams, (result.streams[0], multipart.StreamDescriptor("audio", "aac", sample_rate=48000, channels=6))))

    def test_duration_rejects_bad_values_and_uses_end_time_fallback(self):
        for value in (None, "N/A", "NaN", "Infinity", "-1", "0", "bad"):
            with self.subTest(value=value):
                with self.assertRaises(multipart.MediaProbeError): multipart.parse_ffprobe_payload(self.payload(value))
        payload = self.payload(None); payload["streams"][0].update({"start_time": "2", "duration": "10"}); payload["streams"][1].update({"start_time": "1", "duration": "9"})
        self.assertEqual(multipart.parse_ffprobe_payload(payload).duration_seconds, 12)

    def test_probe_failure_codes_and_safe_command(self):
        captured = {}
        def runner(command, **kwargs): captured.update(command=command, kwargs=kwargs); return SimpleNamespace(returncode=0, stdout='{"format":{"duration":"1"},"streams":[{"codec_type":"video","codec_name":"h264"}]}')
        self.assertEqual(multipart.probe_media("media/video.mp4", runner=runner).duration_seconds, 1)
        self.assertFalse(captured["kwargs"]["shell"]); self.assertIn("-of", captured["command"])
        for error, code in ((FileNotFoundError(), "ffprobe_missing"), (PermissionError(), "ffprobe_unavailable"), (subprocess.TimeoutExpired("ffprobe", 1), "ffprobe_timeout")):
            with self.subTest(code=code):
                with self.assertRaisesRegex(multipart.MediaProbeError, code): multipart.probe_media("x", runner=lambda *args, err=error, **kwargs: (_ for _ in ()).throw(err))


class MultipartMetadataTests(unittest.TestCase):
    def setUp(self): self.base = {"title": "My Stream", "description": "Frozen description", "privacy_status": "private", "category_id": "20", "tags": ["vod"]}
    def test_one_part_preserves_frozen_metadata(self):
        self.assertEqual(multipart.derive_part_upload_plan(self.base, index=1, total=1)["title"], "My Stream")
        self.assertEqual(multipart.derive_part_upload_plan(self.base, index=1, total=1)["description"], "Frozen description")
    def test_multipart_titles_and_description_prefix(self):
        self.assertEqual(multipart.derive_part_upload_plan(self.base, index=1, total=2)["title"], "My Stream (Part 1/2)")
        self.assertEqual(multipart.derive_part_upload_plan(self.base, index=2, total=2)["title"], "My Stream (Part 2/2)")
        self.assertTrue(multipart.derive_part_upload_plan(self.base, index=1, total=12)["title"].endswith("(Part 1/12)"))
    def test_long_unicode_title_and_description_preserve_suffix_and_sanitize(self):
        base = {**self.base, "title": "ä" * 95 + "<", "description": "x" * 6000}
        result = multipart.derive_part_upload_plan(base, index=1, total=2)
        self.assertLessEqual(len(result["title"]), 95); self.assertTrue(result["title"].endswith("(Part 1/2)")); self.assertNotIn("<", result["title"])
        self.assertLessEqual(len(result["description"]), 5000); self.assertTrue(result["description"].startswith("Part 1 of 2."))

