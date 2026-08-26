from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_materialize
from vod_dashboard import job_store
from vod_dashboard import youtube_upload_state
from vod_dashboard.jobs import JobManager
from vod_dashboard.media import MediaPathPolicy


VOD_ID = "2855270041"


class AutoYouTubeMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.ledger = youtube_upload_state.YouTubeUploadStateStore(
            self.root / "youtube-upload-state.json"
        )
        self.jobs_path = self.root / "jobs.json"
        self.manager = self._manager()
        self.path = self._write_media()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manager(self) -> JobManager:
        return JobManager(
            job_store=job_store.JobStore(self.jobs_path),
            media_root=self.media_root,
        )

    def _write_media(self, payload: bytes = b"completed-media") -> Path:
        path = self.media_root / "bearlychen" / "vod.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.with_suffix(".info.json").write_text(
            json.dumps({"id": VOD_ID}), encoding="utf-8"
        )
        return path

    def _ready_record(self, *, playlist_id: str = "PLAYLIST_A") -> dict:
        record, _ = self.ledger.create_intent_if_absent(
            "BearLyChen",
            VOD_ID,
            source_download_job_id="12",
            source_download_item_id="12-item-1",
            media_path="bearlychen/vod.mkv",
            size_bytes=self.path.stat().st_size,
            playlist_id=playlist_id,
            plan_inputs={
                "title_template": "{title}",
                "description_template": "{title}",
                "description_fallback": "",
                "privacy_status": "unlisted",
                "category_id": "20",
                "tags": ["twitch"],
            },
        )
        self.ledger.set_upload_plan("bearlychen", VOD_ID, {
            "title": "Frozen title",
            "description": "Frozen description",
            "privacy_status": "unlisted",
            "category_id": "20",
            "tags": ["twitch"],
        })
        return self.ledger.set_preparation(
            "bearlychen", VOD_ID,
            source_duration_seconds=1.0,
            state="parts_ready",
            split=None,
            parts=[{
                "index": 1, "media_path": "bearlychen/vod.mkv",
                "size_bytes": self.path.stat().st_size,
                "duration_seconds": 1.0, "source_kind": "original",
                "upload_item_id": None, "upload_state": "ready",
                "attempts": 0, "youtube_video_id": None,
                "playlist_state": "pending" if playlist_id else "not_requested",
                "reason": None,
            }],
        )

    def _service(self, manager=None):
        return auto_youtube_materialize.AutoYouTubeMaterializationService(
            state_store=self.ledger,
            job_manager=manager or self.manager,
            media_policy=MediaPathPolicy(self.media_root),
        )

    def test_parts_ready_materializes_one_deferred_job_without_worker_or_api_activity(self):
        self._ready_record()
        with mock.patch.object(self.manager, "start_worker") as start_worker:
            self.assertEqual(self._service().reconcile()["queued"], 1)
        start_worker.assert_not_called()

        record = self.ledger.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "upload_queued")
        self.assertEqual(record["upload_job_id"], "1")
        self.assertEqual(len(record["parts"]), 1)
        self.assertEqual(record["parts"][0]["source_kind"], "original")
        self.assertEqual(record["source_duration_seconds"], 1.0)
        job = self.manager.get_job("1")
        self.assertEqual(job["origin"], "auto_youtube")
        self.assertTrue(job["execution_deferred"])
        self.assertEqual(job["auto_youtube_key"], f"bearlychen:{VOD_ID}")
        self.assertEqual(job["auto_youtube_context"], {
            "streamer": "bearlychen", "twitch_vod_id": VOD_ID,
            "source_download_job_id": "12", "source_download_item_id": "12-item-1",
            "media_path": "bearlychen/vod.mkv",
        })
        self.assertEqual(job["urls"], ["bearlychen/vod.mkv"])
        self.assertEqual(job["playlist_id"], "PLAYLIST_A")
        self.assertEqual(job["item_metadata"][0]["title"], "Frozen title")
        self.assertIsNone(self.manager.claim_next_item("1"))
        self.assertEqual(self.manager.get_job("1")["item_states"], ["queued"])

    def test_reconciliation_restart_and_missing_ledger_attachment_never_duplicate(self):
        self._ready_record()
        service = self._service()
        with mock.patch.object(
            self.ledger, "update_record",
            side_effect=youtube_upload_state.YouTubeUploadStatePersistenceError("full"),
        ):
            self.assertEqual(service.reconcile()["pending"], 1)
        self.assertEqual(self.ledger.get("bearlychen", VOD_ID)["state"], "parts_ready")
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

        self.assertEqual(service.reconcile()["queued"], 1)
        first_id = self.ledger.get("bearlychen", VOD_ID)["upload_job_id"]
        self.assertEqual(first_id, "1")
        self.assertEqual(service.reconcile()["queued"], 1)
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

        restarted = self._manager()
        restored = restarted.restore_from_store()
        self.assertEqual(restored.reconciled_item_count, 0)
        self.assertEqual(self._service(restarted).reconcile()["queued"], 1)
        self.assertEqual(len(restarted.snapshot_jobs()), 1)
        self.assertIsNone(restarted.claim_next_item(first_id))

    def test_manual_upload_can_claim_while_deferred_auto_job_is_skipped(self):
        self._ready_record()
        self._service().reconcile()
        manual_path = self.media_root / "manual.mkv"
        manual_path.write_bytes(b"manual")
        manual = self.manager.create_upload_job(
            [str(manual_path)], "Manual upload", item_metadata=[{
                "streamer": "", "date": "", "title": "Manual", "vod_id": "",
                "name": "manual.mkv", "size_bytes": 6, "size_gb": None,
                "youtube_playlist_id": "",
            }]
        )
        self.assertEqual(self.manager.claim_next_item(manual)["job_id"], manual)
        self.assertEqual(self.manager.get_job("1")["item_states"], ["queued"])

    def test_duplicate_or_conflicting_auto_jobs_fail_closed(self):
        record = self._ready_record()
        source = {
            "streamer": "bearlychen", "twitch_vod_id": VOD_ID,
            "source_download_job_id": "12", "source_download_item_id": "12-item-1",
            "media_path": "bearlychen/vod.mkv", "size_bytes": self.path.stat().st_size,
        }
        plan = record["upload_plan"]
        self.manager.create_auto_youtube_upload_job_deferred(
            source=source, upload_plan=plan, playlist_id="PLAYLIST_A"
        )
        self.manager.create_auto_youtube_upload_job_deferred(
            source=source, upload_plan=plan, playlist_id="PLAYLIST_A"
        )
        self.assertEqual(self._service().reconcile()["attention"], 1)
        current = self.ledger.get("bearlychen", VOD_ID)
        self.assertEqual(current["state"], "needs_attention")
        self.assertEqual(current["reason"], "materialization_consistency_error")

    def test_missing_or_changed_source_never_creates_a_job(self):
        self._ready_record()
        self.path.unlink()
        self.assertEqual(self._service().reconcile()["attention"], 1)
        self.assertEqual(self.ledger.get("bearlychen", VOD_ID)["reason"], "materialization_media_missing")
        self.assertEqual(self.manager.snapshot_jobs(), [])

        self.ledger = youtube_upload_state.YouTubeUploadStateStore(
            self.root / "changed-youtube-upload-state.json"
        )
        self.manager = self._manager()
        self.path = self._write_media()
        self._ready_record()
        self.path.write_bytes(b"different-size")
        self.assertEqual(self._service().reconcile()["attention"], 1)
        self.assertEqual(self.ledger.get("bearlychen", VOD_ID)["reason"], "materialization_source_invalid")
        self.assertEqual(self.manager.snapshot_jobs(), [])

    def test_job_creation_persistence_failure_leaves_parts_ready_and_no_runtime_job(self):
        self._ready_record()
        failing_manager = self._manager()
        with mock.patch.object(
            failing_manager.job_store, "save",
            side_effect=job_store.JobStorePersistenceError("jobs unavailable"),
        ):
            self.assertEqual(self._service(failing_manager).reconcile()["pending"], 1)
        self.assertEqual(self.ledger.get("bearlychen", VOD_ID)["state"], "parts_ready")
        self.assertEqual(failing_manager.snapshot_jobs(), [])

    def test_unhealthy_job_store_leaves_parts_ready_without_creating_a_job(self):
        self._ready_record()
        self.manager._persistence_health["healthy"] = False
        self.assertEqual(self._service().reconcile()["pending"], 1)
        self.assertEqual(self.ledger.get("bearlychen", VOD_ID)["state"], "parts_ready")
        self.assertEqual(self.manager.snapshot_jobs(), [])

    def test_conflicting_job_context_fails_closed_without_selecting_a_job(self):
        record = self._ready_record()
        source = {
            "streamer": "bearlychen", "twitch_vod_id": VOD_ID,
            "source_download_job_id": "13", "source_download_item_id": "13-item-1",
            "media_path": "bearlychen/vod.mkv", "size_bytes": self.path.stat().st_size,
        }
        self.manager.create_auto_youtube_upload_job_deferred(
            source=source, upload_plan=record["upload_plan"], playlist_id="PLAYLIST_A"
        )
        self.assertEqual(self._service().reconcile()["attention"], 1)
        self.assertEqual(
            self.ledger.get("bearlychen", VOD_ID)["reason"],
            "materialization_consistency_error",
        )

    def test_deferred_nonterminal_job_survives_terminal_history_retention(self):
        self._ready_record()
        self._service().reconcile()
        deferred = self.manager.get_job("1")
        self.assertEqual(
            job_store.apply_retention([deferred], terminal_limit=0), [deferred]
        )

    def test_upload_queued_missing_or_conflicting_job_fails_closed(self):
        self._ready_record()
        self._service().reconcile()
        record = self.ledger.get("bearlychen", VOD_ID)
        self.manager.jobs.pop(record["upload_job_id"])
        self.assertEqual(self._service().reconcile()["attention"], 1)
        self.assertEqual(
            self.ledger.get("bearlychen", VOD_ID)["reason"],
            "materialization_consistency_error",
        )
