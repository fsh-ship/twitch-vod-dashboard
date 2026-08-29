from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard.auto_youtube_cleanup import (
    AutoYouTubeCleanupError,
    AutoYouTubeCleanupPeriodicCoordinator,
    AutoYouTubeCleanupService,
    cleanup_status,
)
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStateLoadError,
    YouTubeUploadStateStore,
)


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
            "part_plan_version": 1, "split": ({"mode": "stream_copy", "generation_id": "g1", "target_duration_seconds": 60.0, "target_size_bytes": len(b"source-media"), "split_points_seconds": [], "replan_count": 0} if generated else None),
            "parts": [{"index": 1, "media_path": part_path, "size_bytes": len(b"source-media"), "duration_seconds": 60.0, "source_kind": "generated" if generated else "original", "upload_item_id": "79-item-1", "upload_state": "completed", "attempts": 1, "youtube_video_id": "YT1", "playlist_state": "not_requested", "reason": None}],
            "reason": None, "created_at": "2026-08-29T10:00:00Z", "updated_at": "2026-08-29T12:00:00Z",
            "execution_policy": "automatic",
            "local_cleanup": {"policy": "automatic", "delay_hours": due_hours, "keep_local": keep_local, "cleanup_due_at": due, "cleaned_at": None, "state": "pending", "started_at": None, "canonical_files": [], "generated_files": [], "canonical_status": "pending", "artifacts_status": "pending", "reason": None},
        }

    def save(self, record):
        self.store.replace_state({"version": 5, "uploads": {f"bearlychen:{VOD_ID}": record}})

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
        manual["local_cleanup"] = {"policy": "manual", "delay_hours": None, "keep_local": False, "cleanup_due_at": None, "cleaned_at": None, "state": "pending", "started_at": None, "canonical_files": [], "generated_files": [], "canonical_status": "pending", "artifacts_status": "pending", "reason": None}
        waiting = self.record(state="upload_queued")
        self.assertEqual(cleanup_status(manual, media_policy=self.policy, now=NOW)["state"], "disabled")
        self.assertEqual(cleanup_status(waiting, media_policy=self.policy, now=NOW)["state"], "waiting_for_upload")

    def service(self, **kwargs):
        self.clock[0] = NOW + timedelta(hours=7)
        return AutoYouTubeCleanupService(
            state_store=self.store, media_policy=self.policy,
            clock=lambda: self.clock[0], **kwargs,
        )

    def test_due_single_part_cleanup_removes_only_exact_bundle_and_is_idempotent(self):
        sidecar = self.source.with_suffix(".info.json")
        sidecar.write_text("{}", encoding="utf-8")
        sibling = self.source.parent / "source.notes.txt"
        sibling.write_text("keep", encoding="utf-8")
        self.save(self.record())

        result = self.service().reconcile()

        cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]
        self.assertEqual(result["cleaned"], 1)
        self.assertEqual(cleanup["state"], "completed")
        self.assertIsNotNone(cleanup["cleaned_at"])
        self.assertFalse(self.source.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual(self.service().reconcile()["ignored"], 1)

    def test_multipart_cleanup_removes_exact_parts_but_preserves_unowned_internal_file(self):
        part = self.media / ".auto-youtube" / "bearlychen" / VOD_ID / "g1" / "parts" / "1.mp4"
        part.parent.mkdir(parents=True)
        part.write_bytes(b"source-media")
        unrelated = self.media / ".auto-youtube" / "other" / "keep.mp4"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"keep")
        self.save(self.record(generated=True))

        self.assertEqual(self.service().reconcile()["cleaned"], 1)

        self.assertFalse(self.source.exists())
        self.assertFalse(part.exists())
        self.assertTrue(unrelated.exists())

    def test_missing_before_intent_is_attention_and_never_claimed_cleaned(self):
        self.save(self.record())
        self.source.unlink()

        result = self.service().reconcile()
        cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]

        self.assertEqual(result["attention"], 1)
        self.assertEqual(cleanup["state"], "needs_attention")
        self.assertEqual(cleanup["reason"], "canonical_missing_before_start")
        self.assertIsNone(cleanup["started_at"])
        self.assertIsNone(cleanup["cleaned_at"])

    def test_crash_after_canonical_unlink_resumes_from_durable_intent(self):
        self.save(self.record())
        crashed = {"done": False}

        def unlink_then_crash(path):
            path.unlink()
            if not crashed["done"]:
                crashed["done"] = True
                raise KeyboardInterrupt("simulated crash")

        with self.assertRaises(KeyboardInterrupt):
            self.service(unlink=unlink_then_crash).reconcile()
        cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]
        self.assertEqual(cleanup["state"], "started")
        self.assertIsNotNone(cleanup["started_at"])
        self.assertFalse(self.source.exists())

        self.assertEqual(self.service().reconcile()["cleaned"], 1)
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["local_cleanup"]["state"], "completed")

    def test_crash_after_generated_unlink_resumes_without_broad_deletion(self):
        part = self.media / ".auto-youtube" / "bearlychen" / VOD_ID / "g1" / "parts" / "1.mp4"
        part.parent.mkdir(parents=True)
        part.write_bytes(b"source-media")
        self.save(self.record(generated=True))
        original = self.store.mark_local_cleanup_component

        def crash_before_artifact_checkpoint(*args, **kwargs):
            if kwargs.get("component") == "artifacts":
                raise KeyboardInterrupt("simulated crash")
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "mark_local_cleanup_component", side_effect=crash_before_artifact_checkpoint):
            with self.assertRaises(KeyboardInterrupt):
                self.service().reconcile()
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["local_cleanup"]["state"], "canonical_done")
        self.assertFalse(part.exists())
        self.assertEqual(self.service().reconcile()["cleaned"], 1)

    def test_active_source_and_identity_change_fail_closed_before_unlink(self):
        self.save(self.record())
        active = self.service(active_paths_provider=lambda: {str(self.source)})
        self.assertEqual(active.reconcile()["attention"], 1)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["local_cleanup"]["reason"], "canonical_in_use")

        self.source.write_bytes(b"changed-size-more")
        changed = self.record()
        changed["size_bytes"] = len(b"source-media")
        self.save(changed)
        self.assertEqual(self.service().reconcile()["attention"], 1)
        self.assertTrue(self.source.exists())

    def test_keep_local_is_rejected_once_cleanup_intent_started(self):
        self.save(self.record())

        def crash(path):
            raise KeyboardInterrupt("simulated crash")

        with self.assertRaises(KeyboardInterrupt):
            self.service(unlink=crash).reconcile()
        with self.assertRaisesRegex(Exception, "keep_local_not_allowed"):
            self.store.set_keep_local("bearlychen", VOD_ID, keep_local=True)

    def test_io_failure_blocks_without_claiming_success_or_retrying(self):
        self.save(self.record())
        service = self.service(unlink=mock.Mock(side_effect=PermissionError("denied")))

        self.assertEqual(service.reconcile()["errors"], 1)
        cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]
        self.assertEqual(cleanup["state"], "needs_attention")
        self.assertEqual(cleanup["reason"], "filesystem_error")
        self.assertIsNone(cleanup["cleaned_at"])
        self.assertEqual(service.reconcile()["attention"], 1)

    def test_manual_not_due_keep_and_nonfinal_records_never_unlink(self):
        unlink = mock.Mock()
        manual = self.record()
        manual["local_cleanup"] = {"policy": "manual", "delay_hours": None, "keep_local": False, "cleanup_due_at": None, "cleaned_at": None, "state": "pending", "started_at": None, "canonical_files": [], "generated_files": [], "canonical_status": "pending", "artifacts_status": "pending", "reason": None}
        self.save(manual)
        self.assertEqual(self.service(unlink=unlink).reconcile()["ignored"], 1)

        self.save(self.record(due_hours=48))
        self.assertEqual(self.service(unlink=unlink).reconcile()["pending"], 1)

        self.save(self.record(keep_local=True))
        self.assertEqual(self.service(unlink=unlink).reconcile()["ignored"], 1)

        waiting = self.record(state="playlist_pending")
        waiting["local_cleanup"]["cleanup_due_at"] = None
        self.save(waiting)
        self.assertEqual(self.service(unlink=unlink).reconcile()["ignored"], 1)
        uncertain = self.record(state="needs_attention")
        uncertain["reason"] = "upload_outcome_uncertain"
        uncertain["parts"][0].update({"upload_state": "uncertain", "youtube_video_id": None, "reason": "upload_outcome_uncertain"})
        uncertain["local_cleanup"]["cleanup_due_at"] = None
        self.save(uncertain)
        self.assertEqual(self.service(unlink=unlink).reconcile()["ignored"], 1)
        unlink.assert_not_called()

    def test_playlist_confirmation_is_required_and_confirmed_playlist_can_clean(self):
        completed = self.record()
        completed["playlist_id"] = "PL1"
        completed["parts"][0]["playlist_state"] = "confirmed"
        self.save(completed)
        self.assertEqual(self.service().reconcile()["cleaned"], 1)

    def test_intent_is_durable_before_first_unlink(self):
        self.save(self.record())

        def verify_then_unlink(path):
            cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]
            self.assertEqual(cleanup["state"], "started")
            self.assertTrue(cleanup["canonical_files"])
            path.unlink()

        self.assertEqual(self.service(unlink=verify_then_unlink).reconcile()["cleaned"], 1)

    def test_symlinked_exact_sidecar_fails_closed_when_supported(self):
        target = self.source.parent / "unrelated.json"
        target.write_text("keep", encoding="utf-8")
        sidecar = self.source.with_suffix(".info.json")
        try:
            sidecar.symlink_to(target)
        except OSError:
            self.skipTest("symlinks are unavailable")
        self.save(self.record())

        self.assertEqual(self.service().reconcile()["attention"], 1)
        self.assertTrue(self.source.exists())
        self.assertTrue(target.exists())
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["local_cleanup"]["reason"], "canonical_path_invalid")

    def test_periodic_coordinator_reuses_primary_loop_and_still_checks_after_primary_error(self):
        primary = mock.Mock()
        cleanup = mock.Mock()
        primary.run_once.return_value = {"action": "idle"}
        cleanup.reconcile.return_value = {"cleaned": 0}
        coordinator = AutoYouTubeCleanupPeriodicCoordinator(primary, cleanup)
        self.assertEqual(coordinator.run_once()["auto_youtube_cleanup"], {"cleaned": 0})
        primary.run_once.side_effect = RuntimeError("monitor failure")
        with self.assertRaises(RuntimeError):
            coordinator.run_once()
        self.assertEqual(cleanup.reconcile.call_count, 2)
        primary.run_once.side_effect = KeyboardInterrupt("shutdown")
        with self.assertRaises(KeyboardInterrupt):
            coordinator.run_once()
        self.assertEqual(cleanup.reconcile.call_count, 2)

    def test_durable_manifest_is_cross_checked_against_exact_ledger_ownership(self):
        self.save(self.record())
        self.clock[0] = NOW + timedelta(hours=7)
        service = self.service()
        unrelated = self.media / "bearlychen" / "unrelated.mp4"
        unrelated.write_bytes(b"source-media")

        with self.assertRaises(YouTubeUploadStateLoadError):
            self.store.begin_local_cleanup(
                "bearlychen", VOD_ID,
                canonical_files=[service._manifest_entry(unrelated)],
                generated_files=[],
            )

        self.assertEqual(self.store.get("bearlychen", VOD_ID)["local_cleanup"]["state"], "pending")
        self.assertTrue(self.source.exists())
        self.assertTrue(unrelated.exists())

    def test_non_video_canonical_path_is_rejected_before_intent(self):
        unsafe = self.media / "bearlychen" / "owned.txt"
        unsafe.write_bytes(b"source-media")
        record = self.record()
        record["media_path"] = "bearlychen/owned.txt"
        self.save(record)

        self.assertEqual(self.service().reconcile()["attention"], 1)
        cleanup = self.store.get("bearlychen", VOD_ID)["local_cleanup"]
        self.assertEqual(cleanup["reason"], "canonical_path_invalid")
        self.assertIsNone(cleanup["started_at"])
        self.assertTrue(unsafe.exists())
