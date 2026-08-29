from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        value = {"streamer": "bearlychen", "twitch_vod_id": "2855270041", "source_download_job_id": "38", "source_download_item_id": "38-item-1", "media_path": "bearlychen/video.mp4", "size_bytes": 12, "source_duration_seconds": None, "state": "intent_pending", "upload_job_id": None, "playlist_id": None, "plan_inputs": None, "upload_plan": None, "part_plan_version": None, "split": None, "parts": [], "reason": None, "created_at": "2026-08-26T12:00:00Z", "updated_at": "2026-08-26T12:00:00Z", "execution_policy": "manual", "local_cleanup": {"policy": "manual", "delay_hours": None, "keep_local": False, "cleanup_due_at": None, "cleaned_at": None}}
        value.update(changes)
        return value

    def document(self, **changes):
        value = {"version": state.YOUTUBE_UPLOAD_STATE_VERSION, "uploads": {"bearlychen:2855270041": self.record()}}
        value.update(changes)
        return value

    def test_missing_file_is_healthy_empty_current_schema(self):
        self.assertEqual(self.store.load(), state.empty_youtube_upload_state())
        self.assertEqual(self.store.health(), {"healthy": True, "reason": None})

    def test_prior_schemas_require_explicit_offline_migration(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.path.write_text(json.dumps({"version": version, "uploads": {}}), encoding="utf-8")
                with self.assertRaisesRegex(state.YouTubeUploadStateLoadError, "migration_required"):
                    self.store.load()
                self.assertEqual(self.store.health(), {"healthy": False, "reason": "migration_required"})

    def test_corrupt_and_unsupported_fail_closed(self):
        for raw, reason in ((b"{bad", "invalid_json"), (json.dumps({"version": 5, "uploads": {}}).encode(), "unsupported_version")):
            with self.subTest(reason=reason):
                self.path.write_bytes(raw)
                self.assertEqual(self.store.health(), {"healthy": False, "reason": reason})

    def test_create_uses_v4_record_with_explicit_execution_and_cleanup_policy(self):
        record, created = self.create(execution_policy="automatic")
        self.assertTrue(created); self.assertEqual(record["parts"], [])
        self.assertIsNone(record["source_duration_seconds"])
        self.assertEqual(record["execution_policy"], "automatic")
        self.assertEqual(record["local_cleanup"]["policy"], "manual")
        self.assertEqual(self.store.load()["version"], 4)
        self.assertNotIn(str(self.root), self.path.read_text(encoding="utf-8"))

    def test_execution_policy_is_strict_and_immutable(self):
        with self.assertRaisesRegex(
            state.YouTubeUploadStateValidationError, "invalid_execution_policy"
        ):
            self.create(execution_policy=True)
        record, _ = self.create(execution_policy="automatic")
        duplicate, created = self.create(execution_policy="manual")
        self.assertFalse(created)
        self.assertEqual(duplicate["execution_policy"], "automatic")
        self.assertEqual(record, duplicate)

    def _queued_original(self, *, playlist_id=None, cleanup_delay_hours=0):
        self.create(
            playlist_id=playlist_id,
            cleanup_delay_hours=cleanup_delay_hours,
            plan_inputs={"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []},
        )
        self.store.set_upload_plan("bearlychen", "2855270041", {"title": "title", "description": "", "privacy_status": "private", "category_id": "20", "tags": []})
        self.store.set_preparation(
            "bearlychen", "2855270041", source_duration_seconds=2.0,
            state="parts_ready", split=None,
            parts=[{"index": 1, "media_path": "bearlychen/video.mp4", "size_bytes": 12, "duration_seconds": 2.0, "source_kind": "original", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "pending" if playlist_id else "not_requested", "reason": None}],
        )
        self.store.attach_materialized_upload(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_ids=["99-item-1"],
        )
        self.store.begin_part_transfer(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
        )

    def test_cleanup_policy_is_frozen_and_not_changed_by_duplicate_admission(self):
        first, _ = self.create(cleanup_delay_hours=6)
        duplicate, created = self.create(cleanup_delay_hours=48)
        self.assertFalse(created)
        self.assertEqual(first["local_cleanup"], duplicate["local_cleanup"])
        self.assertEqual(first["local_cleanup"]["delay_hours"], 6)

    def test_playlistless_final_success_schedules_cleanup_deterministically(self):
        self._queued_original(cleanup_delay_hours=6)
        completed = self.store.confirm_part_video(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
            youtube_video_id="video_1",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["local_cleanup"]["cleanup_due_at"], "2026-08-26T18:00:00Z")
        restarted = state.YouTubeUploadStateStore(self.path, clock=lambda: NOW + timedelta(days=1))
        self.assertEqual(restarted.get("bearlychen", "2855270041")["local_cleanup"]["cleanup_due_at"], "2026-08-26T18:00:00Z")

    def test_playlist_pending_never_schedules_until_final_membership_confirmation(self):
        self._queued_original(playlist_id="PL1", cleanup_delay_hours=3)
        pending = self.store.confirm_part_video(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
            youtube_video_id="video_1",
        )
        self.assertEqual(pending["state"], "playlist_pending")
        self.assertIsNone(pending["local_cleanup"]["cleanup_due_at"])
        self.store.begin_part_playlist_insertion(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
        )
        completed = self.store.confirm_part_playlist_membership(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["local_cleanup"]["cleanup_due_at"], "2026-08-26T15:00:00Z")

    def test_uncertain_nonfinal_bundle_never_gets_a_cleanup_deadline(self):
        self._queued_original(cleanup_delay_hours=6)
        attention = self.store.mark_part_attention(
            "bearlychen", "2855270041", upload_job_id="99",
            upload_item_id="99-item-1", part_index=1,
            reason="upload_outcome_uncertain", uncertain=True,
        )
        self.assertEqual(attention["state"], "needs_attention")
        self.assertIsNone(attention["local_cleanup"]["cleanup_due_at"])

    def test_keep_local_persists_and_reversal_gets_a_fresh_full_delay(self):
        clock = [NOW]
        store = state.YouTubeUploadStateStore(self.path, clock=lambda: clock[0])
        self.store = store
        self._queued_original(cleanup_delay_hours=6)
        store.confirm_part_video("bearlychen", "2855270041", upload_job_id="99", upload_item_id="99-item-1", part_index=1, youtube_video_id="video_1")
        kept = store.set_keep_local("bearlychen", "2855270041", keep_local=True)
        self.assertTrue(kept["local_cleanup"]["keep_local"])
        self.assertIsNone(kept["local_cleanup"]["cleanup_due_at"])
        self.assertTrue(state.YouTubeUploadStateStore(self.path).get("bearlychen", "2855270041")["local_cleanup"]["keep_local"])
        clock[0] = NOW + timedelta(days=2)
        resumed = store.set_keep_local("bearlychen", "2855270041", keep_local=False)
        self.assertEqual(resumed["local_cleanup"]["cleanup_due_at"], "2026-08-28T18:00:00Z")

    def test_current_parts_are_strictly_ordered_and_safe(self):
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
