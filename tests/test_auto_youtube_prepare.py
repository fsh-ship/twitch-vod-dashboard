from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_prepare as preparation
from vod_dashboard.auto_youtube_multipart import MediaProbeError, MediaProbeResult, StreamDescriptor
from vod_dashboard.auto_vod_storage import GIB
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import YouTubeUploadStatePersistenceError, YouTubeUploadStateStore


VOD_ID = "2855270041"


class AutoYouTubePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.media_root = self.root / "media"; self.media_root.mkdir()
        self.path = self.media_root / "bearlychen" / "vod.mkv"; self.path.parent.mkdir()
        self.path.write_bytes(b"source-media")
        self.path.with_suffix(".info.json").write_text(json.dumps({"id": VOD_ID}), encoding="utf-8")
        self.store = YouTubeUploadStateStore(self.root / "youtube-upload-state.json")
        self.record, _ = self.store.create_intent_if_absent("bearlychen", VOD_ID, source_download_job_id="12", source_download_item_id="12-item-1", media_path="bearlychen/vod.mkv", size_bytes=self.path.stat().st_size, playlist_id="PL1", plan_inputs={"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []})
        self.record = self.store.set_upload_plan("bearlychen", VOD_ID, {"title": "Frozen", "description": "Frozen description", "privacy_status": "private", "category_id": "20", "tags": []})
        self.streams = (StreamDescriptor("video", "h264", 1920, 1080), StreamDescriptor("audio", "aac", sample_rate=48000, channels=2))

    def tearDown(self): self.temp.cleanup()
    def probe(self, duration): return mock.Mock(return_value=MediaProbeResult(duration, self.streams, ()))
    @staticmethod
    def storage(free=10**15, total=10**15, state="sufficient"):
        return mock.Mock(return_value=SimpleNamespace(state=state, free_bytes=None if state == "unavailable" else free, total_bytes=None if state == "unavailable" else total))
    def service(self, duration, storage=None, probe=None, **dependencies):
        return preparation.AutoYouTubePreparationService(state_store=self.store, media_policy=MediaPathPolicy(self.media_root), probe=probe or self.probe(duration), storage_assessor=storage or self.storage(), **dependencies)

    def test_one_part_persists_original_manifest_without_storage_check_or_metadata_change(self):
        storage = mock.Mock(side_effect=AssertionError("one part must bypass storage"))
        before_plan = dict(self.record["upload_plan"]); media_before = set(self.media_root.rglob("*"))
        self.assertEqual(self.service(11 * 3600 + 59 * 60, storage=storage).prepare_record(self.record), "ready")
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["state"], "parts_ready"); self.assertEqual(saved["source_duration_seconds"], 43140)
        self.assertEqual(saved["upload_plan"], before_plan); self.assertIsNone(saved["split"])
        self.assertEqual(saved["parts"], [{"index": 1, "media_path": "bearlychen/vod.mkv", "size_bytes": self.path.stat().st_size, "duration_seconds": 43140.0, "source_kind": "original", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "pending", "reason": None}])
        self.assertEqual(set(self.media_root.rglob("*")), media_before); storage.assert_not_called()

    def test_multipart_plan_is_deterministic_without_fake_parts_or_directories(self):
        for duration, count in ((43200, 2), (15 * 3600, 2), (24 * 3600, 3)):
            with self.subTest(duration=duration):
                if duration != 43200:
                    self.tearDown(); self.setUp()
                media_before = set(self.media_root.rglob("*"))
                result = self.service(duration).prepare_record(self.record)
                saved = self.store.get("bearlychen", VOD_ID)
                self.assertEqual(result, "preparing"); self.assertEqual(saved["state"], "parts_preparing")
                self.assertEqual(len(saved["split"]["split_points_seconds"]) + 1, count)
                self.assertEqual(saved["parts"], []); self.assertEqual(set(self.media_root.rglob("*")), media_before)
                self.assertFalse((self.media_root / ".auto-youtube").exists())

    def test_size_driven_plan_and_generation_are_restart_stable(self):
        large_size = 256_000_000_000
        self.path.write_bytes(b"x")
        document = self.store.load()
        document["uploads"][f"bearlychen:{VOD_ID}"]["size_bytes"] = large_size
        self.store.replace_state(document)
        self.record = self.store.get("bearlychen", VOD_ID)
        service = self.service(5 * 3600, source_validator=lambda record, policy: self.path, size_reader=lambda path: large_size)
        self.assertEqual(service.prepare_record(self.record), "preparing")
        first = self.store.get("bearlychen", VOD_ID)
        restarted = YouTubeUploadStateStore(self.store.path)
        self.assertEqual(restarted.get("bearlychen", VOD_ID)["split"], first["split"])
        expected_plan = __import__("vod_dashboard.auto_youtube_multipart", fromlist=["plan_multipart_upload"]).plan_multipart_upload(duration_seconds=18000, size_bytes=large_size, signature=self.streams)
        self.assertEqual(first["split"]["generation_id"], preparation.generation_id(self.record, expected_plan))

    def test_exact_storage_formula_and_recoverable_blocking(self):
        total = 400 * GIB; source_size = self.path.stat().st_size
        expected = max(50 * GIB, total * 15 // 100) + (source_size * 105 + 99) // 100 + GIB
        self.assertEqual(preparation.split_required_free_bytes(total, source_size), expected)
        blocked = self.service(43200, storage=self.storage(free=expected - 1, total=total))
        self.assertEqual(blocked.prepare_record(self.record), "blocked")
        first = self.store.get("bearlychen", VOD_ID)
        self.assertEqual((first["state"], first["reason"]), ("needs_attention", "insufficient_storage"))
        generation = first["split"]["generation_id"]
        recovered = self.service(43200, storage=self.storage(free=expected, total=total))
        self.assertEqual(recovered.prepare_record(first), "preparing")
        final = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(final["split"]["generation_id"], generation); self.assertIsNone(final["reason"])

    def test_unavailable_storage_is_durable_and_attempts_do_not_exist(self):
        self.assertEqual(self.service(43200, storage=self.storage(state="unavailable")).prepare_record(self.record), "blocked")
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["reason"], "storage_unavailable"); self.assertNotIn("attempts", saved)

    def test_persistence_failure_exposes_no_partial_preparation(self):
        with mock.patch.object(self.store, "set_preparation", side_effect=YouTubeUploadStatePersistenceError("full")):
            self.assertEqual(self.service(43200).prepare_record(self.record), "pending")
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["state"], "plan_ready"); self.assertIsNone(saved["source_duration_seconds"]); self.assertIsNone(saved["split"])

    def test_source_and_probe_failures_never_create_a_manifest(self):
        cases = []
        self.path.unlink(); cases.append((self.record, self.service(43200), "plan_source_invalid"))
        for record, service, reason in cases:
            self.assertEqual(service.prepare_record(record), "attention"); self.assertEqual(self.store.get("bearlychen", VOD_ID)["reason"], reason)

        self.tearDown(); self.setUp(); self.path.write_bytes(b"changed")
        self.assertEqual(self.service(43200).prepare_record(self.record), "attention")
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["reason"], "plan_source_invalid")

        self.tearDown(); self.setUp(); failed_probe = mock.Mock(side_effect=MediaProbeError("ffprobe_failed"))
        self.assertEqual(self.service(43200, probe=failed_probe).prepare_record(self.record), "attention")
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["reason"], "parts_preparation_failed")

    def test_vod_mismatch_and_path_escape_fail_closed(self):
        self.path.with_suffix(".info.json").write_text(json.dumps({"id": "2855270042"}), encoding="utf-8")
        self.assertEqual(self.service(43200).prepare_record(self.record), "attention")
        escaped = dict(self.record, media_path="../escape.mkv")
        self.assertEqual(self.service(43200).prepare_record(escaped), "attention")

    def test_service_has_no_job_api_ffmpeg_or_worker_dependency(self):
        service = self.service(43200)
        self.assertFalse(any(hasattr(service, name) for name in ("job_manager", "youtube", "ffmpeg", "worker")))
