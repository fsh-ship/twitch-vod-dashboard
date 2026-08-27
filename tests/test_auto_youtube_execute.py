from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard.auto_youtube_execute import (
    AutoYouTubeExecutionError,
    AutoYouTubeExecutionService,
)
from vod_dashboard.auto_youtube_materialize import AutoYouTubeMaterializationService
from vod_dashboard.auto_youtube_multipart import MediaProbeResult
from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import JobManager, JobPersistenceRequiredError
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube import (
    create_resumable_video_upload_request,
    send_resumable_video_upload_request,
)
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


VOD_ID = "2855270041"


class AutoYouTubeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.store = YouTubeUploadStateStore(self.root / "youtube-upload-state.json")
        self.jobs_path = self.root / "jobs.json"
        self.manager = self.new_manager()
        self.probe_durations = {}
        self.service_getter = mock.Mock(return_value=mock.Mock(name="youtube-service"))
        self.request_builder = mock.Mock(side_effect=lambda _service, path, body, _settings: {"path": path, "body": body})
        self.request_sender = mock.Mock(return_value="YT_VIDEO_1")
        self.settings = {
            "youtube_privacy_status": "public",
            "youtube_category_id": "99",
            "youtube_tags": "changed,current,settings",
            "youtube_playlist_id": "CURRENT_PLAYLIST",
        }

    def tearDown(self):
        self.temp.cleanup()

    def new_manager(self):
        return JobManager(
            job_store=JobStore(self.jobs_path), media_root=self.media_root
        )

    def probe(self, path: Path) -> MediaProbeResult:
        relative = path.relative_to(self.media_root).as_posix()
        return MediaProbeResult(self.probe_durations.get(relative, 1.0), (), ())

    def create_bundle(self, total=1, playlist_id=""):
        source_path = self.media_root / "bearlychen" / "source.mkv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"source-media")
        source_path.with_suffix(".info.json").write_text(
            json.dumps({"id": VOD_ID}), encoding="utf-8"
        )
        self.probe_durations["bearlychen/source.mkv"] = float(total if total == 1 else 1)
        self.store.create_intent_if_absent(
            "bearlychen", VOD_ID,
            source_download_job_id="12", source_download_item_id="12-item-1",
            media_path="bearlychen/source.mkv", size_bytes=source_path.stat().st_size,
            playlist_id=playlist_id or None,
            plan_inputs={
                "title_template": "{title}", "description_template": "{title}",
                "description_fallback": "", "privacy_status": "unlisted",
                "category_id": "20", "tags": ["frozen", "tag"],
            },
        )
        frozen = {
            "title": "Frozen title", "description": "Frozen description\nLine two",
            "privacy_status": "unlisted", "category_id": "20",
            "tags": ["frozen", "tag"],
        }
        self.store.set_upload_plan("bearlychen", VOD_ID, frozen)
        if total == 1:
            parts = [{
                "index": 1, "media_path": "bearlychen/source.mkv",
                "size_bytes": source_path.stat().st_size,
                "duration_seconds": 1.0, "source_kind": "original",
                "upload_item_id": None, "upload_state": "ready", "attempts": 0,
                "youtube_video_id": None,
                "playlist_state": "pending" if playlist_id else "not_requested",
                "reason": None,
            }]
            split = None
        else:
            parts = []
            for index in range(1, total + 1):
                relative = f".auto-youtube/bearlychen/{VOD_ID}/g1/parts/part-{index:03d}.mkv"
                path = self.media_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"part-{index}".encode())
                self.probe_durations[relative] = 1.0
                parts.append({
                    "index": index, "media_path": relative,
                    "size_bytes": path.stat().st_size,
                    "duration_seconds": 1.0, "source_kind": "generated",
                    "upload_item_id": None, "upload_state": "ready", "attempts": 0,
                    "youtube_video_id": None,
                    "playlist_state": "pending" if playlist_id else "not_requested",
                    "reason": None,
                })
            split = {
                "mode": "stream_copy", "generation_id": "g1",
                "target_duration_seconds": 42300,
                "target_size_bytes": 250000000000,
                "split_points_seconds": [float(value) for value in range(1, total)],
            }
        self.store.set_preparation(
            "bearlychen", VOD_ID, source_duration_seconds=float(total),
            state="parts_ready", split=split, parts=parts,
        )
        materializer = AutoYouTubeMaterializationService(
            state_store=self.store, job_manager=self.manager,
            media_policy=MediaPathPolicy(self.media_root), probe=self.probe,
        )
        self.assertEqual(materializer.reconcile()["queued"], 1)
        return self.store.get("bearlychen", VOD_ID)["upload_job_id"]

    def executor(self, manager=None):
        return AutoYouTubeExecutionService(
            state_store=self.store, job_manager=manager or self.manager,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=self.service_getter,
            request_builder=self.request_builder,
            request_sender=self.request_sender,
            probe=self.probe,
        )

    def test_deferred_production_style_job_remains_inert_through_reconcile_and_worker_opportunity(self):
        job_id = self.create_bundle()
        executor = self.executor()

        self.assertEqual(executor.reconcile()["deferred"], 1)
        executor.run_job(job_id)

        job = self.manager.get_job(job_id)
        self.assertTrue(job["execution_deferred"])
        self.assertEqual(job["item_states"], ["queued"])
        self.assertIsNone(self.manager.claim_next_item(job_id))
        self.service_getter.assert_not_called()
        self.request_builder.assert_not_called()
        self.request_sender.assert_not_called()

    def test_release_is_explicit_durable_and_rejects_manual_job(self):
        job_id = self.create_bundle()
        self.assertTrue(self.executor().release_auto_youtube_job_for_execution(job_id))
        self.assertFalse(self.manager.get_job(job_id)["execution_deferred"])
        restored = self.new_manager()
        restored.restore_from_store()
        self.assertFalse(restored.get_job(job_id)["execution_deferred"])
        self.service_getter.assert_not_called()

        manual = self.manager.create_upload_job(["bearlychen/source.mkv"], "Manual")
        with self.assertRaisesRegex(AutoYouTubeExecutionError, "invalid_auto_youtube_job"):
            self.executor().release_auto_youtube_job_for_execution(manual)

    def test_release_persistence_failure_leaves_job_deferred_without_api_activity(self):
        job_id = self.create_bundle()
        with mock.patch.object(
            self.manager.job_store, "save",
            side_effect=JobStorePersistenceError("full"),
        ):
            with self.assertRaises(JobPersistenceRequiredError):
                self.executor().release_auto_youtube_job_for_execution(job_id)
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()

    def test_release_rejects_conflicting_or_uncertain_ledger(self):
        job_id = self.create_bundle()
        self.store.update_record(
            "bearlychen", VOD_ID, upload_job_id="999"
        )
        with self.assertRaisesRegex(AutoYouTubeExecutionError, "ownership_mismatch"):
            self.executor().release_auto_youtube_job_for_execution(job_id)
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])

        self.tearDown(); self.setUp()
        job_id = self.create_bundle()
        item_id = self.manager.get_job(job_id)["item_ids"][0]
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=item_id, part_index=1,
        )
        self.store.mark_part_attention(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=item_id, part_index=1,
            reason="upload_outcome_uncertain", uncertain=True,
        )
        with self.assertRaisesRegex(AutoYouTubeExecutionError, "release_not_allowed"):
            self.executor().release_auto_youtube_job_for_execution(job_id)
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
        self.request_sender.assert_not_called()

    def test_transfer_boundary_and_frozen_single_part_body_ordering(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        events = []

        def build(_service, path, body, _settings):
            record = self.store.get("bearlychen", VOD_ID)
            events.append(("request", record["parts"][0]["upload_state"]))
            return {"path": path, "body": body}

        def send(request, **_kwargs):
            record = self.store.get("bearlychen", VOD_ID)
            events.append(("send", record["parts"][0]["upload_state"], record["parts"][0]["attempts"]))
            self.assertEqual(request["body"], {
                "snippet": {
                    "title": "Frozen title",
                    "description": "Frozen description\nLine two",
                    "tags": ["frozen", "tag"], "categoryId": "20",
                },
                "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
            })
            self.assertNotIn("playlistId", json.dumps(request["body"]))
            return "YT_VIDEO_1"

        executor._request_builder = build
        executor._request_sender = send
        original_complete = self.manager.complete_auto_youtube_item

        def complete(*args, **kwargs):
            record = self.store.get("bearlychen", VOD_ID)
            events.append(("complete", record["parts"][0]["upload_state"], record["parts"][0]["youtube_video_id"]))
            return original_complete(*args, **kwargs)

        with mock.patch.object(self.manager, "complete_auto_youtube_item", side_effect=complete):
            executor.run_job(job_id)

        self.assertEqual(events, [
            ("request", "queued"),
            ("send", "transfer_started", 1),
            ("complete", "video_confirmed", "YT_VIDEO_1"),
        ])
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])

    def test_transfer_start_persistence_failure_sends_nothing_and_consumes_no_attempt(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        with mock.patch.object(
            self.store, "begin_part_transfer",
            side_effect=RuntimeError("disk full"),
        ):
            executor.run_job(job_id)

        part = self.store.get("bearlychen", VOD_ID)["parts"][0]
        self.assertEqual((part["upload_state"], part["attempts"]), ("queued", 0))
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
        self.request_builder.assert_called_once()
        self.request_sender.assert_not_called()

    def test_missing_media_and_oauth_failure_block_before_transfer(self):
        for failure in ("missing", "changed_size", "oauth"):
            with self.subTest(failure=failure):
                self.tearDown(); self.setUp()
                job_id = self.create_bundle()
                executor = self.executor()
                executor.release_auto_youtube_job_for_execution(job_id)
                if failure == "missing":
                    (self.media_root / "bearlychen" / "source.mkv").unlink()
                elif failure == "changed_size":
                    (self.media_root / "bearlychen" / "source.mkv").write_bytes(
                        b"changed-source-size"
                    )
                else:
                    self.service_getter.side_effect = RuntimeError("offline")
                executor.run_job(job_id)
                record = self.store.get("bearlychen", VOD_ID)
                self.assertEqual(record["state"], "needs_attention")
                self.assertEqual(record["parts"][0]["attempts"], 0)
                self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
                self.request_sender.assert_not_called()

    def test_post_transfer_exception_and_malformed_success_become_uncertain(self):
        for outcome in ("exception", "missing_id"):
            with self.subTest(outcome=outcome):
                self.tearDown(); self.setUp()
                job_id = self.create_bundle(3)
                executor = self.executor()
                executor.release_auto_youtube_job_for_execution(job_id)
                self.request_sender.side_effect = RuntimeError("timeout") if outcome == "exception" else None
                self.request_sender.return_value = None if outcome == "missing_id" else "unused"
                executor.run_job(job_id)
                record = self.store.get("bearlychen", VOD_ID)
                self.assertEqual(record["parts"][0]["upload_state"], "uncertain")
                self.assertEqual(record["parts"][0]["attempts"], 1)
                self.assertEqual([part["upload_state"] for part in record["parts"][1:]], ["queued", "queued"])
                job = self.manager.get_job(job_id)
                self.assertTrue(job["execution_deferred"])
                self.assertEqual(job["item_states"], ["failed", "queued", "queued"])
                self.assertEqual(self.request_sender.call_count, 1)

    def test_remote_success_with_confirmation_failure_never_completes_or_reuploads(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        with mock.patch.object(
            self.store, "confirm_part_video", side_effect=RuntimeError("disk full")
        ):
            executor.run_job(job_id)
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["parts"][0]["upload_state"], "uncertain")
        self.assertIsNone(record["parts"][0]["youtube_video_id"])
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["failed"])
        executor.run_job(job_id)
        self.assertEqual(self.request_sender.call_count, 1)

    def test_multipart_executes_in_order_and_stops_at_playlist_pending(self):
        job_id = self.create_bundle(3, playlist_id="FROZEN_PLAYLIST")
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        sent_titles = []
        sent_descriptions = []

        def send(request, **_kwargs):
            sent_titles.append(request["body"]["snippet"]["title"])
            sent_descriptions.append(request["body"]["snippet"]["description"])
            return f"YT_{len(sent_titles)}"

        executor._request_sender = send
        executor.run_job(job_id)
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(sent_titles, [
            "Frozen title (Part 1/3)", "Frozen title (Part 2/3)",
            "Frozen title (Part 3/3)",
        ])
        self.assertEqual(sent_descriptions, [
            f"Part {index} of 3.\n\nFrozen description\nLine two"
            for index in (1, 2, 3)
        ])
        self.assertEqual([part["youtube_video_id"] for part in record["parts"]], ["YT_1", "YT_2", "YT_3"])
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual([part["playlist_state"] for part in record["parts"]], ["pending", "pending", "pending"])
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed", "completed", "completed"])

    def test_restart_recovery_uses_ledger_boundary_without_api_activity(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        restarted = self.new_manager(); restarted.restore_from_store()
        restarted_executor = self.executor(restarted)
        self.assertEqual(restarted.get_job(job_id)["item_states"], ["running"])
        restarted_executor.reconcile()
        self.assertEqual(restarted.get_job(job_id)["item_states"], ["queued"])
        self.assertFalse(restarted.get_job(job_id)["execution_deferred"])
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()
        self.assertEqual(claim["item_id"], f"{job_id}-item-1")

    def test_restart_transfer_started_becomes_uncertain_and_blocks(self):
        job_id = self.create_bundle(2)
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=claim["item_id"], part_index=1,
        )

        restarted = self.new_manager(); restarted.restore_from_store()
        self.executor(restarted).reconcile()

        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["parts"][0]["upload_state"], "uncertain")
        self.assertEqual(record["parts"][1]["upload_state"], "queued")
        job = restarted.get_job(job_id)
        self.assertTrue(job["execution_deferred"])
        self.assertEqual(job["item_states"], ["failed", "queued"])
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()

    def test_restart_confirmed_video_repairs_jobstore_completion_without_upload(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=claim["item_id"], part_index=1,
        )
        self.store.confirm_part_video(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=claim["item_id"], part_index=1,
            youtube_video_id="CONFIRMED_ID",
        )

        restarted = self.new_manager(); restarted.restore_from_store()
        self.executor(restarted).reconcile()

        self.assertEqual(restarted.get_job(job_id)["item_states"], ["completed"])
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["parts"][0]["youtube_video_id"],
            "CONFIRMED_ID",
        )
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()

    def test_jobstore_completion_without_ledger_confirmation_fails_closed(self):
        job_id = self.create_bundle()
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        self.manager.complete_auto_youtube_item(job_id, claim["item_id"])

        restarted = self.new_manager(); restarted.restore_from_store()
        self.executor(restarted).reconcile()

        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(record["parts"][0]["upload_state"], "failed_known")
        self.assertTrue(restarted.get_job(job_id)["execution_deferred"])
        self.request_sender.assert_not_called()

    def test_no_flask_route_exposes_the_internal_release_primitive(self):
        app_source = (Path(__file__).parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".release_auto_youtube_job_for_execution(", app_source)


class ResumablePrimitiveTests(unittest.TestCase):
    def test_request_construction_does_not_send_and_sender_starts_with_next_chunk(self):
        service = mock.Mock()
        request = service.videos.return_value.insert.return_value
        request.next_chunk.return_value = (None, {"id": "YT1"})
        media_factory = mock.Mock(return_value=mock.Mock())
        body = {"snippet": {"title": "Frozen"}, "status": {"privacyStatus": "private"}}

        built = create_resumable_video_upload_request(
            Path("video.mp4"), body, service=service,
            media_upload_factory=media_factory, chunk_size_mb=8,
        )
        request.next_chunk.assert_not_called()
        self.assertIs(built, request)

        self.assertEqual(
            send_resumable_video_upload_request(request), "YT1"
        )
        request.next_chunk.assert_called_once_with()
        service.playlistItems.assert_not_called()


if __name__ == "__main__":
    unittest.main()
