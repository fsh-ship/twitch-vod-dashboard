from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from vod_dashboard import youtube_upload_state as state


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class YouTubeUploadStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / state.YOUTUBE_UPLOAD_STATE_FILE_NAME
        self.store = state.YouTubeUploadStateStore(self.path, clock=lambda: NOW)

    def tearDown(self): self.temp.cleanup()

    def create(self, **changes):
        values = {"streamer": "BearLyChen", "twitch_vod_id": "2855270041", "source_download_job_id": "38", "source_download_item_id": "38-item-1", "media_path": "bearlychen/video.mp4", "size_bytes": 12}
        values.update(changes)
        return self.store.create_intent_if_absent(**values)

    def record(self, **changes):
        value = {"streamer": "bearlychen", "twitch_vod_id": "2855270041", "source_download_job_id": "38", "source_download_item_id": "38-item-1", "media_path": "bearlychen/video.mp4", "size_bytes": 12, "source_duration_seconds": None, "state": "intent_pending", "upload_job_id": None, "playlist_id": None, "plan_inputs": None, "upload_plan": None, "part_plan_version": None, "split": None, "parts": [], "reason": None, "created_at": "2026-08-26T12:00:00Z", "updated_at": "2026-08-26T12:00:00Z"}
        value.update(changes)
        return value

    def document(self, **changes):
        value = {"version": 2, "uploads": {"bearlychen:2855270041": self.record()}}
        value.update(changes)
        return value

    def test_missing_file_is_healthy_empty_v2(self):
        self.assertEqual(self.store.load(), state.empty_youtube_upload_state())
        self.assertEqual(self.store.health(), {"healthy": True, "reason": None})

    def test_v1_requires_explicit_offline_migration(self):
        self.path.write_text(json.dumps({"version": 1, "uploads": {}}), encoding="utf-8")
        with self.assertRaisesRegex(state.YouTubeUploadStateLoadError, "migration_required"):
            self.store.load()
        self.assertEqual(self.store.health(), {"healthy": False, "reason": "migration_required"})

    def test_corrupt_and_unsupported_fail_closed(self):
        for raw, reason in ((b"{bad", "invalid_json"), (json.dumps({"version": 3, "uploads": {}}).encode(), "unsupported_version")):
            with self.subTest(reason=reason):
                self.path.write_bytes(raw)
                self.assertEqual(self.store.health(), {"healthy": False, "reason": reason})

    def test_create_uses_v2_record_without_part_manifest(self):
        record, created = self.create()
        self.assertTrue(created); self.assertEqual(record["parts"], [])
        self.assertIsNone(record["source_duration_seconds"])
        self.assertEqual(self.store.load()["version"], 2)
        self.assertNotIn(str(self.root), self.path.read_text(encoding="utf-8"))

    def test_v2_parts_are_strictly_ordered_and_safe(self):
        good = {"index": 1, "media_path": "bearlychen/part-1.mp4", "size_bytes": 5, "duration_seconds": 2.5, "source_kind": "generated", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "not_requested", "reason": None}
        self.path.write_text(json.dumps(self.document(uploads={"bearlychen:2855270041": self.record(parts=[good], part_plan_version=1)})), encoding="utf-8")
        self.assertEqual(self.store.load()["uploads"]["bearlychen:2855270041"]["parts"], [good])
        invalid = [dict(good, index=2), dict(good, source_kind="raw"), dict(good, media_path="../part.mp4"), dict(good, media_path="C:/part.mp4"), dict(good, upload_state="bad"), dict(good, playlist_state="bad")]
        for part in invalid:
            with self.subTest(part=part):
                self.path.write_text(json.dumps(self.document(uploads={"bearlychen:2855270041": self.record(parts=[part], part_plan_version=1)})), encoding="utf-8")
                with self.assertRaises(state.YouTubeUploadStateLoadError): self.store.load()

    def test_split_metadata_is_strict(self):
        part = {"index": 1, "media_path": "bearlychen/part.mp4", "size_bytes": 5, "duration_seconds": 2.5, "source_kind": "generated", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "not_requested", "reason": None}
        split = {"mode": "stream_copy", "generation_id": "g1", "target_duration_seconds": 60, "target_size_bytes": 100, "split_points_seconds": [30, 20]}
        self.path.write_text(json.dumps(self.document(uploads={"bearlychen:2855270041": self.record(parts=[part], part_plan_version=1, split=split)})), encoding="utf-8")
        with self.assertRaises(state.YouTubeUploadStateLoadError): self.store.load()

    def test_split_replan_count_is_bounded_and_old_v2_defaults_to_zero(self):
        split = {
            "mode": "stream_copy", "generation_id": "g1",
            "target_duration_seconds": 60,
            "target_size_bytes": 100,
            "split_points_seconds": [30],
        }
        record = self.record(
            state="parts_preparing", source_duration_seconds=60,
            part_plan_version=1, split=split,
        )
        self.path.write_text(json.dumps(self.document(
            uploads={"bearlychen:2855270041": record}
        )), encoding="utf-8")
        self.assertEqual(
            self.store.load()["uploads"]["bearlychen:2855270041"]["split"]["replan_count"],
            0,
        )
        for invalid in (-1, state.MAX_AUTOMATIC_REPLANS + 1, True, "1"):
            with self.subTest(invalid=invalid):
                changed = dict(split, replan_count=invalid)
                self.path.write_text(json.dumps(self.document(uploads={
                    "bearlychen:2855270041": dict(record, split=changed)
                })), encoding="utf-8")
                with self.assertRaises(state.YouTubeUploadStateLoadError):
                    self.store.load()

    def test_current_p8_plan_and_deferred_link_remain_supported(self):
        self.create(plan_inputs={"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []})
        ready = self.store.set_upload_plan("bearlychen", "2855270041", {"title": "title", "description": "", "privacy_status": "private", "category_id": "20", "tags": []})
        queued = self.store.update_record("bearlychen", "2855270041", state="upload_queued", upload_job_id="99")
        self.assertEqual(ready["state"], "plan_ready")
        self.assertEqual(queued["upload_job_id"], "99")

    def test_attach_materialized_upload_links_every_part_in_one_transition(self):
        self.create(plan_inputs={"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []})
        self.store.set_upload_plan("bearlychen", "2855270041", {"title": "title", "description": "", "privacy_status": "private", "category_id": "20", "tags": []})
        parts = [
            {"index": index, "media_path": f".auto-youtube/bearlychen/2855270041/g1/parts/{index}.mp4", "size_bytes": 5, "duration_seconds": 1.0, "source_kind": "generated", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "not_requested", "reason": None}
            for index in (1, 2)
        ]
        self.store.set_preparation(
            "bearlychen", "2855270041", source_duration_seconds=2.0,
            state="parts_ready",
            split={"mode": "stream_copy", "generation_id": "g1", "target_duration_seconds": 60, "target_size_bytes": 100, "split_points_seconds": [1.0]},
            parts=parts,
        )
        queued = self.store.attach_materialized_upload(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_ids=["99-item-1", "99-item-2"],
        )
        self.assertEqual(queued["state"], "upload_queued")
        self.assertEqual(queued["upload_job_id"], "99")
        self.assertEqual(
            [part["upload_item_id"] for part in queued["parts"]],
            ["99-item-1", "99-item-2"],
        )
        self.assertEqual([part["upload_state"] for part in queued["parts"]], ["queued", "queued"])
        with self.assertRaises(state.YouTubeUploadStateValidationError):
            self.store.attach_materialized_upload(
                "bearlychen", "2855270041", upload_job_id="100",
                upload_item_ids=["100-item-1", "100-item-2"],
            )

    def test_create_rejects_unsafe_source_identity(self):
        for change in ({"twitch_vod_id": "v2855270041"}, {"media_path": "../video.mp4"}, {"media_path": "/srv/video.mp4"}, {"size_bytes": -1}):
            with self.subTest(change=change):
                with self.assertRaises(state.YouTubeUploadStateValidationError): self.create(**change)
