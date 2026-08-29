from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard.auto_youtube_cleanup import (
    AutoYouTubeCleanupError,
    AutoYouTubeCleanupService,
    cleanup_status,
)
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
VOD_ID = "2855270041"


class AutoYouTubeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.source = self.media / "bearlychen" / "source.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"source-media")
        self.clock = [NOW]
        self.store = YouTubeUploadStateStore(
            self.root / "youtube-upload-state.json",
            clock=lambda: self.clock[0],
        )
        self.policy = MediaPathPolicy(self.media)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, *, state="completed", due_hours=6, keep_local=False, generated=False):
        due = None if keep_local or state != "completed" else (
            NOW + timedelta(hours=due_hours)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        part_path = ".auto-youtube/bearlychen/2855270041/g1/parts/1.mp4" if generated else "bearlychen/source.mp4"
        return {
            "streamer": "bearlychen", "twitch_vod_id": VOD_ID,
            "source_download_job_id": "38", "source_download_item_id": "38-item-1",
            "media_path": "bearlychen/source.mp4", "size_bytes": len(b"source-media"),
            "source_duration_seconds": 60.0, "state": state, "upload_job_id": "79",
            "playlist_id": None, "plan_inputs": None, "upload_plan": None,
            "part_plan_version": 1, "split": None,
            "parts": [{"index": 1, "media_path": part_path, "size_bytes": len(b"source-media"), "duration_seconds": 60.0, "source_kind": "generated" if generated else "original", "upload_item_id": "79-item-1", "upload_state": "completed", "attempts": 1, "youtube_video_id": "YT1", "playlist_state": "not_requested", "reason": None}],
            "reason": None, "created_at": "2026-08-29T10:00:00Z", "updated_at": "2026-08-29T12:00:00Z",
            "execution_policy": "automatic",
            "local_cleanup": {"policy": "automatic", "delay_hours": due_hours, "keep_local": keep_local, "cleanup_due_at": due, "cleaned_at": None},
        }

    def save(self, record):
        self.store.replace_state({"version": 4, "uploads": {f"bearlychen:{VOD_ID}": record}})

    def test_status_is_read_only_and_never_deletes_media(self):
        record = self.record()
        with mock.patch.object(Path, "unlink", side_effect=AssertionError("must not delete")):
            status = cleanup_status(record, media_policy=self.policy, now=NOW)
            overdue = cleanup_status(record, media_policy=self.policy, now=NOW + timedelta(hours=7))
        self.assertEqual(status["state"], "scheduled")
        self.assertTrue(status["can_keep_local"])
        self.assertEqual(overdue["state"], "due")
        self.assertTrue(self.source.exists())

    def test_missing_canonical_source_is_reported_without_using_generated_parts(self):
        record = self.record(generated=True)
        self.assertEqual(cleanup_status(record, media_policy=self.policy, now=NOW)["state"], "scheduled")
        self.source.unlink()
        self.assertEqual(cleanup_status(record, media_policy=self.policy, now=NOW)["state"], "local_copy_missing")

    def test_keep_local_mutation_requires_exact_owner_and_media(self):
        self.save(self.record())
        service = AutoYouTubeCleanupService(state_store=self.store, media_policy=self.policy)
        with self.assertRaisesRegex(AutoYouTubeCleanupError, "ownership_not_found"):
            service.set_keep_local("other", VOD_ID, media_path=self.source, keep_local=True)
        forged = self.media / "bearlychen" / "other.mp4"
        forged.write_bytes(b"source-media")
        with self.assertRaisesRegex(AutoYouTubeCleanupError, "ownership_mismatch"):
            service.set_keep_local("bearlychen", VOD_ID, media_path=forged, keep_local=True)
        kept = service.set_keep_local("bearlychen", VOD_ID, media_path=self.source, keep_local=True)
        self.assertTrue(kept["local_cleanup"]["keep_local"])
        self.assertEqual(cleanup_status(kept, media_policy=self.policy, now=NOW)["state"], "keep_local")

    def test_manual_policy_and_nonfinal_automatic_policy_are_not_eligible(self):
        manual = self.record()
        manual["local_cleanup"] = {"policy": "manual", "delay_hours": None, "keep_local": False, "cleanup_due_at": None, "cleaned_at": None}
        waiting = self.record(state="upload_queued")
        self.assertEqual(cleanup_status(manual, media_policy=self.policy, now=NOW)["state"], "disabled")
        self.assertEqual(cleanup_status(waiting, media_policy=self.policy, now=NOW)["state"], "waiting_for_upload")
