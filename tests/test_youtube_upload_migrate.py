from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from vod_dashboard.youtube_upload_migrate import (
    YouTubeUploadMigrationError, convert_v1_state, run_migration,
)
from vod_dashboard.youtube_upload_state import YOUTUBE_UPLOAD_STATE_FILE_NAME


class YouTubeUploadMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.path = self.root / YOUTUBE_UPLOAD_STATE_FILE_NAME

    def tearDown(self): self.temp.cleanup()

    def record(self, **changes):
        value = {"streamer": "bearlychen", "twitch_vod_id": "2855270041", "source_download_job_id": "38", "source_download_item_id": "38-item-1", "media_path": "bearlychen/video.mp4", "size_bytes": 12, "state": "plan_ready", "upload_job_id": None, "attempts": 0, "youtube_video_id": None, "playlist_id": "PL1", "playlist_state": "pending", "reason": None, "created_at": "2026-08-26T12:00:00Z", "updated_at": "2026-08-26T12:00:00Z", "plan_inputs": {"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []}, "upload_plan": {"title": "title", "description": "", "privacy_status": "private", "category_id": "20", "tags": []}}
        value.update(changes); return value

    def write(self, record=None):
        self.path.write_text(json.dumps({"version": 1, "uploads": {"bearlychen:2855270041": record or self.record()}}), encoding="utf-8")

    def test_dry_run_writes_nothing_and_preserves_deferred_link_for_later_preparation(self):
        self.write(self.record(state="upload_queued", upload_job_id="88")); before = self.path.read_bytes()
        report = run_migration(self.root, apply=False)
        self.assertEqual(report.action, "dry_run"); self.assertEqual(report.multipart_preparation_required, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_apply_creates_backup_and_atomically_replaces_only_ledger(self):
        self.write(); report = run_migration(self.root, apply=True, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(report.action, "applied"); self.assertTrue(Path(report.backup_path).is_dir())
        converted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(converted["version"], 2); self.assertEqual(converted["uploads"]["bearlychen:2855270041"]["playlist_id"], "PL1")
        self.assertEqual(converted["uploads"]["bearlychen:2855270041"]["upload_plan"]["title"], "title")
        self.assertTrue((Path(report.backup_path) / YOUTUBE_UPLOAD_STATE_FILE_NAME).exists())

    def test_confirmed_video_is_never_lost(self):
        converted, report = convert_v1_state({"version": 1, "uploads": {"bearlychen:2855270041": self.record(state="completed", youtube_video_id="video_1", attempts=2, playlist_state="confirmed")}})
        part = converted["uploads"]["bearlychen:2855270041"]["parts"][0]
        self.assertEqual(report.confirmed_video_ids_preserved, 1); self.assertEqual(part["youtube_video_id"], "video_1")
        self.assertEqual(part["source_kind"], "original"); self.assertEqual(part["attempts"], 2)

    def test_inconsistent_or_corrupt_source_fails_without_replacement(self):
        self.path.write_bytes(b"{bad"); before = self.path.read_bytes()
        with self.assertRaises(YouTubeUploadMigrationError): run_migration(self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps({"version": 2, "uploads": {}}), encoding="utf-8")
        self.assertEqual(run_migration(self.root, apply=True).action, "already_migrated")

