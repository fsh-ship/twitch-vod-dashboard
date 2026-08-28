from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_handoff as handoff
from vod_dashboard import job_store
from vod_dashboard import youtube_upload_state
from vod_dashboard.jobs import JobManager, JobPersistenceRequiredError


VOD_ID = "2855270041"


class AutoYouTubeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = youtube_upload_state.YouTubeUploadStateStore(
            self.root / "youtube-upload-state.json"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _completed_job(self, *, streamer: str = "bearlychen", manager=None):
        manager = manager or JobManager()
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: bearlychen",
            origin="auto_vod",
            streamer=streamer,
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        item_id = manager.claim_next_item(job_id)["item_id"]
        return manager, job_id, item_id

    @staticmethod
    def _settings(*, global_enabled=False, streamer_enabled=False, playlist=""):
        profile = {}
        if streamer_enabled:
            profile["auto_youtube_upload"] = True
        if playlist:
            profile["youtube_playlist_id"] = playlist
        return {
            "auto_youtube_enabled": global_enabled,
            "streamer_profiles": {"bearlychen": profile},
        }

    def _finish(self, manager, job_id, item_id, settings):
        decision = handoff.completion_admission(settings, "bearlychen")
        manager.finish_auto_vod_download_with_result(
            job_id,
            item_id,
            completed_media_path="bearlychen/final.mkv",
            completed_media_size_bytes=123,
            completed_twitch_vod_id=VOD_ID,
            auto_youtube_handoff=decision.handoff,
            auto_youtube_handoff_reason=decision.reason,
            auto_youtube_playlist_id=decision.playlist_id,
            auto_youtube_execution_policy=decision.execution_policy,
        )
        return decision

    def _service(self, manager):
        return handoff.AutoYouTubeHandoffService(
            job_manager=manager, state_store=self.store
        )

    def test_global_disabled_is_durable_not_eligible_without_intent(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(manager, job_id, item_id, self._settings(streamer_enabled=True))

        job = manager.get_job(job_id)
        self.assertEqual(job["item_auto_youtube_handoffs"], ["not_eligible"])
        self.assertEqual(job["item_auto_youtube_handoff_reasons"], ["global_disabled"])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self._service(manager).reconcile()["ignored"], 1)
        self.assertFalse(self.store.path.exists())

    def test_streamer_disabled_and_legacy_auto_flags_never_admit(self):
        for settings in (
            self._settings(global_enabled=True),
            {"youtube_auto_upload": True, "streamer_profiles": {"bearlychen": {"auto_youtube_upload": True}}},
            {"auto_vod_enabled": True, "streamer_profiles": {"bearlychen": {"auto_youtube_upload": True}}},
        ):
            with self.subTest(settings=settings):
                manager, job_id, item_id = self._completed_job()
                self._finish(manager, job_id, item_id, settings)
                job = manager.get_job(job_id)
                self.assertEqual(job["item_auto_youtube_handoffs"], ["not_eligible"])
                self.assertFalse(self.store.path.exists())

    def test_eligible_completion_creates_one_frozen_intent_without_upload_work(self):
        manager, job_id, item_id = self._completed_job(streamer="BearLyChen")
        self._finish(
            manager,
            job_id,
            item_id,
            self._settings(
                global_enabled=True, streamer_enabled=True, playlist="PLAYLIST_A"
            ),
        )

        outcome = self._service(manager).admit_pending(job_id, item_id)
        job = manager.get_job(job_id)
        record = self.store.get("bearlychen", VOD_ID)

        self.assertEqual(outcome, "created")
        self.assertEqual(job["item_auto_youtube_handoffs"], ["intent_created"])
        self.assertEqual(record["source_download_job_id"], job_id)
        self.assertEqual(record["source_download_item_id"], item_id)
        self.assertEqual(record["media_path"], "bearlychen/final.mkv")
        self.assertEqual(record["size_bytes"], 123)
        self.assertEqual(record["playlist_id"], "PLAYLIST_A")
        self.assertEqual(record["execution_policy"], "automatic")
        self.assertEqual(record["playlist_id"], "PLAYLIST_A")
        self.assertIsNone(record["upload_job_id"])
        self.assertEqual(record["parts"], [])
        self.assertEqual(record["parts"], [])
        self.assertEqual(
            [job.get("type", "download") for job in manager.snapshot_jobs()],
            ["download"],
        )

    def test_no_playlist_is_frozen_as_not_requested(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        self._service(manager).admit_pending(job_id, item_id)
        record = self.store.get("bearlychen", VOD_ID)
        self.assertIsNone(record["playlist_id"])
        self.assertIsNone(record["playlist_id"])

    def test_historical_or_not_eligible_jobs_are_never_backfilled(self):
        manager, job_id, item_id = self._completed_job()
        manager.finish_auto_vod_download_with_result(
            job_id,
            item_id,
            completed_media_path="bearlychen/historical.mkv",
            completed_media_size_bytes=123,
            completed_twitch_vod_id=VOD_ID,
        )
        service = self._service(manager)
        self.assertEqual(service.reconcile()["ignored"], 1)
        self.assertFalse(self.store.path.exists())

        manager, job_id, item_id = self._completed_job()
        self._finish(manager, job_id, item_id, self._settings())
        self.assertEqual(self._service(manager).reconcile()["ignored"], 1)
        self.assertFalse(self.store.path.exists())

    def test_manual_download_is_not_an_auto_youtube_admission_source(self):
        manager = JobManager()
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"], "Manual download"
        )
        item_id = manager.claim_next_item(job_id)["item_id"]
        manager.finish_claimed_item(job_id, item_id, "completed")

        self.assertEqual(self._service(manager).reconcile(), {
            "created": 0,
            "blocked": 0,
            "pending": 0,
            "ignored": 0,
        })
        self.assertFalse(self.store.path.exists())

    def test_reconciliation_creates_pending_once_and_is_idempotent(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        service = self._service(manager)
        self.assertEqual(service.reconcile()["created"], 1)
        first = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(service.reconcile()["ignored"], 1)
        self.assertEqual(self.store.get("bearlychen", VOD_ID), first)

    def test_existing_ledger_after_marker_persistence_failure_recovers(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        service = self._service(manager)
        with mock.patch.object(
            manager,
            "set_auto_youtube_handoff",
            side_effect=RuntimeError("job store unavailable"),
        ):
            self.assertEqual(service.admit_pending(job_id, item_id), "pending")
        self.assertIsNotNone(self.store.get("bearlychen", VOD_ID))
        self.assertEqual(manager.get_job(job_id)["item_auto_youtube_handoffs"], ["intent_pending"])
        self.assertEqual(service.reconcile()["created"], 1)

    def test_missing_or_conflicting_owner_fails_closed(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        manager.set_auto_youtube_handoff(job_id, item_id, "intent_created")
        self.assertEqual(self._service(manager).reconcile()["blocked"], 1)
        self.assertEqual(manager.get_job(job_id)["item_auto_youtube_handoffs"], ["handoff_blocked"])
        self.assertEqual(manager.get_job(job_id)["item_auto_youtube_handoff_reasons"], ["intent_missing"])

        first_manager, first_job, first_item = self._completed_job()
        self._finish(first_manager, first_job, first_item, self._settings(global_enabled=True, streamer_enabled=True))
        self._service(first_manager).admit_pending(first_job, first_item)
        second_manager, second_job, second_item = self._completed_job(
            manager=first_manager
        )
        self._finish(second_manager, second_job, second_item, self._settings(global_enabled=True, streamer_enabled=True))
        self.assertEqual(self._service(second_manager).admit_pending(second_job, second_item), "blocked")
        self.assertEqual(second_manager.get_job(second_job)["item_auto_youtube_handoff_reasons"], ["intent_conflict"])

    def test_corrupt_or_failing_upload_state_never_changes_twitch_completion(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        self.store.path.write_text("{bad", encoding="utf-8")
        self.assertEqual(self._service(manager).admit_pending(job_id, item_id), "blocked")
        job = manager.get_job(job_id)
        self.assertEqual(job["item_states"], ["completed"])
        self.assertEqual(job["item_auto_youtube_handoffs"], ["handoff_blocked"])
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{bad")

        manager, job_id, item_id = self._completed_job()
        self._finish(manager, job_id, item_id, self._settings(global_enabled=True, streamer_enabled=True))
        clean_store = youtube_upload_state.YouTubeUploadStateStore(self.root / "failing.json")
        with mock.patch.object(clean_store, "create_intent_if_absent", side_effect=youtube_upload_state.YouTubeUploadStatePersistenceError("no space")):
            service = handoff.AutoYouTubeHandoffService(job_manager=manager, state_store=clean_store)
            self.assertEqual(service.admit_pending(job_id, item_id), "pending")
        self.assertEqual(manager.get_job(job_id)["item_auto_youtube_handoffs"], ["intent_pending"])

    def test_job_store_handoff_validation_and_pending_retention_are_strict(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(manager, job_id, item_id, self._settings(global_enabled=True, streamer_enabled=True))
        job = manager.get_job(job_id)
        self.assertEqual(job_store.serialize_job(job)["item_auto_youtube_handoffs"], ["intent_pending"])
        invalid = dict(job)
        invalid["item_auto_youtube_handoff_reasons"] = ["global_disabled"]
        with self.assertRaises(job_store.JobStoreValidationError):
            job_store.serialize_job(invalid)
        retained = job_store.apply_retention([job], terminal_limit=0)
        self.assertEqual([value["id"] for value in retained], [job_id])
        self.assertEqual(self._service(manager).admit_pending(job_id, item_id), "created")
        self.assertEqual(job_store.apply_retention([manager.get_job(job_id)], terminal_limit=0), [])
        self.assertIsNotNone(self.store.get("bearlychen", VOD_ID))

    def test_corrupt_completed_result_blocks_without_creating_an_owner(self):
        manager, job_id, item_id = self._completed_job()
        self._finish(
            manager, job_id, item_id,
            self._settings(global_enabled=True, streamer_enabled=True),
        )
        manager.jobs[job_id]["completed_twitch_vod_id"] = "2855270042"

        self.assertEqual(self._service(manager).admit_pending(job_id, item_id), "blocked")
        self.assertEqual(manager.get_job(job_id)["item_auto_youtube_handoff_reasons"], ["invalid_completed_result"])
        self.assertFalse(self.store.path.exists())

    def test_required_source_persistence_failure_prevents_any_ledger_call(self):
        durable_store = job_store.JobStore(self.root / "jobs.json")
        manager = JobManager(job_store=durable_store, media_root=self.root)
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: bearlychen",
            origin="auto_vod",
            streamer="bearlychen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        item_id = manager.claim_next_item(job_id)["item_id"]
        decision = handoff.completion_admission(
            self._settings(global_enabled=True, streamer_enabled=True),
            "bearlychen",
        )
        with mock.patch.object(durable_store, "save", side_effect=OSError("ENOSPC")):
            with self.assertRaises(JobPersistenceRequiredError):
                manager.finish_auto_vod_download_with_result(
                    job_id,
                    item_id,
                    completed_media_path="bearlychen/final.mkv",
                    completed_media_size_bytes=123,
                    completed_twitch_vod_id=VOD_ID,
                    auto_youtube_handoff=decision.handoff,
                    auto_youtube_handoff_reason=decision.reason,
                    auto_youtube_playlist_id=decision.playlist_id,
                    auto_youtube_execution_policy=decision.execution_policy,
                )
        self.assertFalse(self.store.path.exists())
        self.assertEqual(manager.get_job(job_id)["item_states"], ["failed"])
