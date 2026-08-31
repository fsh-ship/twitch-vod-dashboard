from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from vod_dashboard.auto_youtube_execute import (
    AutoYouTubeExecutionError,
    AutoYouTubeExecutionService,
)
from vod_dashboard.auto_youtube_materialize import AutoYouTubeMaterializationService
from vod_dashboard.auto_youtube_multipart import MediaProbeResult
from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import (
    JobManager,
    JobPersistenceRequiredError,
    UploadWorkerDependencies,
    run_upload_job,
)
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube import (
    create_resumable_video_upload_request,
    send_resumable_video_upload_request,
)
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStatePersistenceError,
    YouTubeUploadStateStore,
)


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

    def create_bundle(
        self,
        total=1,
        playlist_id="",
        *,
        streamer="bearlychen",
        vod_id=VOD_ID,
        execution_policy="manual",
    ):
        source_path = self.media_root / streamer / "source.mkv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"source-media")
        source_path.with_suffix(".info.json").write_text(
            json.dumps({"id": vod_id}), encoding="utf-8"
        )
        source_relative = f"{streamer}/source.mkv"
        self.probe_durations[source_relative] = float(total if total == 1 else 1)
        self.store.create_intent_if_absent(
            streamer, vod_id,
            source_download_job_id="12", source_download_item_id="12-item-1",
            media_path=source_relative, size_bytes=source_path.stat().st_size,
            playlist_id=playlist_id or None,
            execution_policy=execution_policy,
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
        self.store.set_upload_plan(streamer, vod_id, frozen)
        if total == 1:
            parts = [{
                "index": 1, "media_path": source_relative,
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
                relative = f".auto-youtube/{streamer}/{vod_id}/g1/parts/part-{index:03d}.mkv"
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
            streamer, vod_id, source_duration_seconds=float(total),
            state="parts_ready", split=split, parts=parts,
        )
        materializer = AutoYouTubeMaterializationService(
            state_store=self.store, job_manager=self.manager,
            media_policy=MediaPathPolicy(self.media_root), probe=self.probe,
        )
        self.assertGreaterEqual(materializer.reconcile()["queued"], 1)
        return self.store.get(streamer, vod_id)["upload_job_id"]

    def executor(self, manager=None, *, playlist_chainer=None):
        return AutoYouTubeExecutionService(
            state_store=self.store, job_manager=manager or self.manager,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=self.service_getter,
            request_builder=self.request_builder,
            request_sender=self.request_sender,
            probe=self.probe,
            playlist_chainer=playlist_chainer,
        )

    def make_uncertain(self, job_id, *, part_index=1):
        executor = self.executor()
        if self.manager.get_job(job_id)["execution_deferred"]:
            executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        self.assertEqual(claim["index"], part_index - 1)
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID,
            upload_job_id=job_id, upload_item_id=claim["item_id"],
            part_index=part_index,
        )
        self.store.mark_part_attention(
            "bearlychen", VOD_ID,
            upload_job_id=job_id, upload_item_id=claim["item_id"],
            part_index=part_index, reason="upload_outcome_uncertain",
            uncertain=True,
        )
        self.manager.block_auto_youtube_item(
            job_id, claim["item_id"], uncertain=True,
            reason="upload_outcome_uncertain",
        )
        return claim["item_id"]

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

    def test_new_automatic_one_part_bundle_releases_and_executes_once(self):
        job_id = self.create_bundle(
            playlist_id="FROZEN_PLAYLIST", execution_policy="automatic"
        )
        transfer_states = []

        def send_after_transfer(*_args, **_kwargs):
            transfer_states.append(
                self.store.get("bearlychen", VOD_ID)["parts"][0]["upload_state"]
            )
            return "YT_VIDEO_1"

        self.request_sender.side_effect = send_after_transfer

        def finish_playlist(current_job_id):
            current = self.store.get("bearlychen", VOD_ID)
            item_id = current["parts"][0]["upload_item_id"]
            self.store.begin_part_playlist_insertion(
                "bearlychen", VOD_ID,
                upload_job_id=current_job_id,
                upload_item_id=item_id,
                part_index=1,
            )
            self.store.confirm_part_playlist_membership(
                "bearlychen", VOD_ID,
                upload_job_id=current_job_id,
                upload_item_id=item_id,
                part_index=1,
            )

        playlist_chainer = mock.Mock(side_effect=finish_playlist)
        executor = self.executor(playlist_chainer=playlist_chainer)
        workers = []

        result = executor.release_automatic_jobs_for_execution(
            lambda current: workers.append(
                self.manager.start_worker(executor.run_job, current)
            )
        )
        workers[0].join(2.0)

        self.assertEqual(result["released"], 1)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        self.assertFalse(self.manager.get_job(job_id)["execution_deferred"])
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["youtube_video_id"], "YT_VIDEO_1")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        self.assertEqual(transfer_states, ["transfer_started"])
        playlist_chainer.assert_called_once_with(job_id)
        self.request_sender.assert_called_once()
        repeated = executor.release_automatic_jobs_for_execution(
            lambda current: workers.append(
                self.manager.start_worker(executor.run_job, current)
            )
        )
        self.assertEqual(repeated["released"], 0)
        self.request_sender.assert_called_once()

    def test_new_automatic_multipart_bundle_uses_one_worker_and_ordered_parts(self):
        job_id = self.create_bundle(
            total=2,
            playlist_id="FROZEN_PLAYLIST",
            execution_policy="automatic",
        )
        self.request_sender.side_effect = ["YT_PART_1", "YT_PART_2"]

        def finish_playlist(current_job_id):
            current = self.store.get("bearlychen", VOD_ID)
            for index, part in enumerate(current["parts"], 1):
                self.store.begin_part_playlist_insertion(
                    "bearlychen", VOD_ID,
                    upload_job_id=current_job_id,
                    upload_item_id=part["upload_item_id"],
                    part_index=index,
                )
                self.store.confirm_part_playlist_membership(
                    "bearlychen", VOD_ID,
                    upload_job_id=current_job_id,
                    upload_item_id=part["upload_item_id"],
                    part_index=index,
                )

        playlist_chainer = mock.Mock(side_effect=finish_playlist)
        executor = self.executor(playlist_chainer=playlist_chainer)
        starter = mock.Mock(side_effect=executor.run_job)

        result = executor.release_automatic_jobs_for_execution(starter)

        self.assertEqual(result["released"], 1)
        starter.assert_called_once_with(job_id)
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed", "completed"])
        self.assertEqual(
            [part["youtube_video_id"] for part in self.store.get("bearlychen", VOD_ID)["parts"]],
            ["YT_PART_1", "YT_PART_2"],
        )
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["state"], "completed")
        playlist_chainer.assert_called_once_with(job_id)
        self.assertEqual(self.request_sender.call_count, 2)

    def test_manual_policy_is_never_retroactively_automatic(self):
        job_id = self.create_bundle(execution_policy="manual")
        starter = mock.Mock()

        result = self.executor().release_automatic_jobs_for_execution(starter)

        self.assertEqual(result["released"], 0)
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
        starter.assert_not_called()
        self.service_getter.assert_not_called()

    def test_automatic_release_persistence_failure_never_starts_worker(self):
        job_id = self.create_bundle(execution_policy="automatic")
        starter = mock.Mock()
        with mock.patch.object(
            self.manager.job_store,
            "save",
            side_effect=JobStorePersistenceError("full"),
        ):
            result = self.executor().release_automatic_jobs_for_execution(starter)

        self.assertEqual(result["pending"], 1)
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])
        starter.assert_not_called()
        self.service_getter.assert_not_called()

    def test_startup_recovers_durably_released_automatic_job_without_new_lineage(self):
        job_id = self.create_bundle(execution_policy="automatic")
        self.executor().release_auto_youtube_job_for_execution(job_id)
        restarted = self.new_manager()
        restarted.restore_from_store()
        executor = self.executor(restarted)
        executor.reconcile()
        starter = mock.Mock()

        result = executor.release_automatic_jobs_for_execution(
            starter, recover_released=True
        )

        self.assertEqual(result["recovered"], 1)
        starter.assert_called_once_with(job_id)
        self.assertEqual(len(restarted.snapshot_jobs()), 1)
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()

    def test_startup_releases_deferred_automatic_job_once(self):
        job_id = self.create_bundle(execution_policy="automatic")
        restarted = self.new_manager()
        restarted.restore_from_store()
        executor = self.executor(restarted)
        executor.reconcile()
        starter = mock.Mock()

        result = executor.release_automatic_jobs_for_execution(
            starter, recover_released=True
        )
        repeated = executor.release_automatic_jobs_for_execution(
            starter, recover_released=True
        )

        self.assertEqual(result["released"], 1)
        self.assertEqual(repeated["already_started"], 1)
        starter.assert_called_once_with(job_id)
        self.assertFalse(restarted.get_job(job_id)["execution_deferred"])
        self.assertEqual(len(restarted.snapshot_jobs()), 1)
        self.service_getter.assert_not_called()
        self.request_sender.assert_not_called()

    def test_paused_upload_queue_blocks_automatic_worker_until_resume(self):
        job_id = self.create_bundle(execution_policy="automatic")
        sent = threading.Event()
        self.request_sender.side_effect = lambda *_args, **_kwargs: (
            sent.set() or "YT_VIDEO_1"
        )
        executor = self.executor()
        self.manager.pause_queue("youtube_upload")
        workers = []

        result = executor.release_automatic_jobs_for_execution(
            lambda current: workers.append(
                self.manager.start_worker(executor.run_job, current)
            )
        )

        self.assertEqual(result["released"], 1)
        self.assertFalse(sent.wait(0.1))
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["queued"])
        self.manager.resume_queue("youtube_upload")
        self.assertTrue(sent.wait(2.0))
        workers[0].join(2.0)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])

    def test_successful_auto_upload_without_frozen_playlist_does_not_chain_playlist_work(self):
        job_id = self.create_bundle(playlist_id="")
        playlist_chainer = mock.Mock()
        executor = self.executor(playlist_chainer=playlist_chainer)

        self.assertTrue(executor.release_auto_youtube_job_for_execution(job_id))
        executor.run_job(job_id)

        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        playlist_chainer.assert_not_called()
        self.request_sender.assert_called_once()

    def test_job_79_style_release_is_durable_and_changes_only_selected_bundle(self):
        self.manager.counter = 78
        job_79 = self.create_bundle(
            streamer="cptmary", vod_id="2856000079"
        )
        other_job = self.create_bundle(
            streamer="bearlychen", vod_id="2856000080"
        )
        self.assertEqual(job_79, "79")

        self.assertTrue(
            self.executor().release_auto_youtube_job_for_execution(job_79)
        )

        self.assertFalse(self.manager.get_job(job_79)["execution_deferred"])
        self.assertTrue(
            self.manager.get_job(other_job)["execution_deferred"]
        )
        restored = self.new_manager()
        restored.restore_from_store()
        self.assertFalse(restored.get_job(job_79)["execution_deferred"])
        self.assertTrue(restored.get_job(other_job)["execution_deferred"])
        self.service_getter.assert_not_called()
        self.request_builder.assert_not_called()
        self.request_sender.assert_not_called()

    def test_job_79_release_worker_dispatch_uses_ledger_executor_exclusively(self):
        self.manager.counter = 78
        job_id = self.create_bundle(
            playlist_id="FROZEN_PLAYLIST",
            streamer="cptmary",
            vod_id="2857167152",
        )
        self.assertEqual(job_id, "79")
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        manual_upload = mock.Mock(return_value="LEGACY_ID")
        manual_service = mock.Mock()
        manual_path = mock.Mock()
        sender = mock.Mock()

        def send(_request, **_kwargs):
            durable = YouTubeUploadStateStore(self.store.path).get(
                "cptmary", "2857167152"
            )
            self.assertEqual(durable["parts"][0]["upload_state"], "transfer_started")
            self.assertEqual(durable["parts"][0]["attempts"], 1)
            self.assertIsNone(durable["parts"][0]["youtube_video_id"])
            return "YT_JOB_79"

        sender.side_effect = send
        executor._request_sender = sender
        dependencies = UploadWorkerDependencies(
            load_settings=mock.Mock(),
            append_log=self.manager.append_job_log,
            get_youtube_service=manual_service,
            safe_local_video_path=manual_path,
            upload_to_youtube=manual_upload,
            auto_youtube_executor=executor.run_job,
        )

        class ImmediateThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self):
                self.target(*self.args)

        self.manager.start_worker(
            lambda current_job_id: run_upload_job(
                current_job_id, self.manager, dependencies
            ),
            job_id,
            thread_factory=ImmediateThread,
        )

        record = YouTubeUploadStateStore(self.store.path).get(
            "cptmary", "2857167152"
        )
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["attempts"], 1)
        self.assertEqual(record["parts"][0]["youtube_video_id"], "YT_JOB_79")
        job = self.manager.get_job(job_id)
        self.assertEqual(job["item_states"], ["completed"])
        self.assertFalse(job["execution_deferred"])
        sender.assert_called_once()
        manual_upload.assert_not_called()
        manual_service.assert_not_called()
        manual_path.assert_not_called()
        dependencies.load_settings.assert_not_called()
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

        run_upload_job(job_id, self.manager, dependencies)
        sender.assert_called_once()
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

        restored = self.new_manager()
        restored.restore_from_store()
        restarted_executor = self.executor(restored)
        restarted_executor.reconcile()
        restarted_executor.reconcile()
        self.assertEqual(restored.get_job(job_id)["item_states"], ["completed"])
        self.assertIsNone(restored.claim_next_item(job_id))
        with self.assertRaisesRegex(AutoYouTubeExecutionError, "release_not_allowed"):
            restarted_executor.release_auto_youtube_job_for_execution(job_id)
        sender.assert_called_once()
        self.assertEqual(len(restored.snapshot_jobs()), 1)

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

    def test_confirmed_video_repairs_stale_queued_job_without_second_upload(self):
        self.manager.counter = 78
        job_id = self.create_bundle(
            playlist_id="FROZEN_PLAYLIST",
            streamer="cptmary",
            vod_id="2856000079",
        )
        self.assertEqual(job_id, "79")
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        claim = self.manager.claim_next_item(job_id)
        self.store.begin_part_transfer(
            "cptmary", "2856000079", upload_job_id=job_id,
            upload_item_id=claim["item_id"], part_index=1,
        )
        self.store.confirm_part_video(
            "cptmary", "2856000079", upload_job_id=job_id,
            upload_item_id=claim["item_id"], part_index=1,
            youtube_video_id="YT_CONFIRMED",
        )
        self.assertTrue(
            self.manager.reset_auto_youtube_item_to_queued(
                job_id, claim["item_id"]
            )
        )
        self.assertEqual(self.manager.get_job(job_id)["state"], "queued")
        self.assertEqual(
            self.store.get("cptmary", "2856000079")["state"],
            "playlist_pending",
        )

        restarted = self.new_manager()
        restarted.restore_from_store()
        restarted_executor = self.executor(restarted)
        self.assertEqual(
            restarted_executor.reconcile()["confirmed"], 1
        )
        completed = restarted.get_job(job_id)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["item_states"], ["completed"])
        self.assertFalse(completed["execution_deferred"])
        self.assertEqual(
            restarted.snapshot_jobs()[0]["state"], "completed"
        )
        record = self.store.get("cptmary", "2856000079")
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual(record["upload_job_id"], job_id)
        self.assertEqual(record["parts"][0]["youtube_video_id"], "YT_CONFIRMED")

        before = restarted.get_job(job_id)
        self.assertEqual(restarted_executor.reconcile()["confirmed"], 1)
        self.assertEqual(restarted.get_job(job_id), before)
        for _attempt in range(2):
            with self.assertRaises(AutoYouTubeExecutionError) as raised:
                restarted_executor.release_auto_youtube_job_for_execution(job_id)
            self.assertEqual(str(raised.exception), "release_not_allowed")

        restarted.pause_queue("youtube_upload")
        restarted.resume_queue("youtube_upload")
        self.assertIsNone(restarted.claim_next_item(job_id))
        restarted_executor.run_job(job_id)
        self.assertEqual(
            [job["id"] for job in restarted.snapshot_jobs()], [job_id]
        )
        self.service_getter.assert_not_called()
        self.request_builder.assert_not_called()
        self.request_sender.assert_not_called()

    def test_confirmed_multipart_repair_finalizes_only_the_confirmed_part(self):
        job_id = self.create_bundle(3)
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        item_ids = self.manager.get_job(job_id)["item_ids"]
        claim = self.manager.claim_next_item(job_id)
        self.assertEqual(claim["item_id"], item_ids[0])
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=item_ids[0], part_index=1,
        )
        self.store.confirm_part_video(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=item_ids[0], part_index=1,
            youtube_video_id="YT_1",
        )
        self.assertTrue(self.manager.defer_auto_youtube_job(job_id))

        restarted = self.new_manager()
        restarted.restore_from_store()
        result = self.executor(restarted).reconcile()
        self.assertEqual(result["confirmed"], 1)
        job = restarted.get_job(job_id)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["item_states"], ["completed", "queued", "queued"])
        self.assertTrue(job["execution_deferred"])
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "upload_queued")
        self.assertEqual(
            [part["upload_state"] for part in record["parts"]],
            ["video_confirmed", "queued", "queued"],
        )
        self.assertEqual(
            [part["youtube_video_id"] for part in record["parts"]],
            ["YT_1", None, None],
        )
        self.request_sender.assert_not_called()

    def test_reconcile_requires_confirmed_youtube_video_id_for_completion(self):
        job_id = self.create_bundle()
        executor = self.executor()
        job = self.manager.get_job(job_id)
        record = self.store.get("bearlychen", VOD_ID)
        record["parts"][0]["upload_state"] = "video_confirmed"
        record["parts"][0]["youtube_video_id"] = None

        with mock.patch.object(
            executor, "_ownership", return_value=(job, record, [{}])
        ):
            result = executor.reconcile()

        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["queued"])
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

    def test_reviewed_uncertain_part_is_eligible_and_reuses_normal_execution(self):
        job_id = self.create_bundle(playlist_id="FROZEN_PLAYLIST")
        item_id = self.make_uncertain(job_id)
        executor = self.executor()

        status = executor.recovery_status_for_jobs(
            self.manager.snapshot_jobs()
        )
        self.assertEqual(
            status[job_id]["eligible_item_ids"], [item_id]
        )
        result = executor.recover_uncertain_part_for_execution(
            job_id, item_id
        )

        self.assertEqual(result["part_index"], 1)
        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "upload_queued")
        self.assertEqual(record["playlist_id"], "FROZEN_PLAYLIST")
        self.assertEqual(record["parts"][0]["upload_state"], "queued")
        self.assertIsNone(record["parts"][0]["youtube_video_id"])
        job = self.manager.get_job(job_id)
        self.assertFalse(job["execution_deferred"])
        self.assertEqual(job["item_states"], ["queued"])

        executor.run_job(job_id)

        self.assertEqual(self.request_sender.call_count, 1)
        self.assertEqual(
            self.manager.get_job(job_id)["item_states"], ["completed"]
        )
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["parts"][0][
                "youtube_video_id"
            ],
            "YT_VIDEO_1",
        )

    def test_uncertain_recovery_preserves_confirmed_multipart_prefix(self):
        job_id = self.create_bundle(2, playlist_id="FROZEN_PLAYLIST")
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        first = self.manager.claim_next_item(job_id)
        self.store.begin_part_transfer(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=first["item_id"], part_index=1,
        )
        self.store.confirm_part_video(
            "bearlychen", VOD_ID, upload_job_id=job_id,
            upload_item_id=first["item_id"], part_index=1,
            youtube_video_id="CONFIRMED_PART_1",
        )
        self.manager.complete_auto_youtube_item(job_id, first["item_id"])
        second_id = self.make_uncertain(job_id, part_index=2)

        executor.recover_uncertain_part_for_execution(job_id, second_id)
        executor.run_job(job_id)

        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(
            [part["youtube_video_id"] for part in record["parts"]],
            ["CONFIRMED_PART_1", "YT_VIDEO_1"],
        )
        self.assertEqual(self.request_sender.call_count, 1)
        uploaded_path = self.request_builder.call_args.args[1]
        self.assertEqual(uploaded_path.name, "part-002.mkv")
        self.assertEqual(record["playlist_id"], "FROZEN_PLAYLIST")

    def test_uncertain_recovery_rejects_wrong_state_identity_and_known_video_id(self):
        job_id = self.create_bundle()
        item_id = self.make_uncertain(job_id)
        executor = self.executor()

        with self.assertRaisesRegex(
            AutoYouTubeExecutionError, "ownership_mismatch"
        ):
            executor.recover_uncertain_part_for_execution(
                job_id, f"{job_id}-item-999"
            )

        with self.manager.lock:
            self.manager.jobs[job_id]["item_failure_kinds"][0] = "known"
        with self.assertRaisesRegex(
            AutoYouTubeExecutionError, "recovery_not_allowed"
        ):
            executor.recover_uncertain_part_for_execution(job_id, item_id)
        with self.manager.lock:
            self.manager.jobs[job_id]["item_failure_kinds"][0] = "uncertain"

        document = self.store.load()
        key = f"bearlychen:{VOD_ID}"
        document["uploads"][key]["parts"][0]["youtube_video_id"] = (
            "KNOWN_REMOTE_ID"
        )
        self.store.replace_state(document)
        with self.assertRaisesRegex(
            AutoYouTubeExecutionError, "video_already_confirmed"
        ):
            executor.recover_uncertain_part_for_execution(job_id, item_id)
        self.request_sender.assert_not_called()

    def test_uncertain_recovery_is_single_use_and_persistence_failures_are_closed(self):
        job_id = self.create_bundle()
        item_id = self.make_uncertain(job_id)
        executor = self.executor()
        with mock.patch.object(
            self.manager, "_persist_required",
            side_effect=JobPersistenceRequiredError(
                "persistence_unavailable"
            ),
        ):
            with self.assertRaises(JobPersistenceRequiredError):
                executor.recover_uncertain_part_for_execution(job_id, item_id)
        self.assertEqual(
            self.manager.get_job(job_id)["item_states"], ["failed"]
        )
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["parts"][0][
                "upload_state"
            ],
            "uncertain",
        )

        with mock.patch.object(
            self.store, "recover_uncertain_part",
            side_effect=YouTubeUploadStatePersistenceError("disk full"),
        ):
            with self.assertRaisesRegex(
                AutoYouTubeExecutionError, "recovery_persistence_failed"
            ):
                executor.recover_uncertain_part_for_execution(job_id, item_id)
        self.assertEqual(
            self.manager.get_job(job_id)["item_states"], ["failed"]
        )
        self.assertTrue(
            self.manager.get_job(job_id)["execution_deferred"]
        )
        self.request_sender.assert_not_called()

        executor.recover_uncertain_part_for_execution(job_id, item_id)
        with self.assertRaisesRegex(
            AutoYouTubeExecutionError, "recovery_not_allowed"
        ):
            executor.recover_uncertain_part_for_execution(job_id, item_id)
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

    def test_confirmed_video_repairs_after_jobstore_completion_failure_without_reupload(self):
        job_id = self.create_bundle(playlist_id="FROZEN_PLAYLIST")
        executor = self.executor()
        executor.release_auto_youtube_job_for_execution(job_id)
        with mock.patch.object(
            self.manager,
            "complete_auto_youtube_item",
            side_effect=JobPersistenceRequiredError(),
        ):
            executor.run_job(job_id)

        record = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["youtube_video_id"], "YT_VIDEO_1")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["running"])
        self.assertEqual(self.request_sender.call_count, 1)

        restored = self.new_manager()
        restored.restore_from_store()
        restarted_executor = self.executor(restored)
        self.assertEqual(restarted_executor.reconcile()["confirmed"], 1)
        self.assertEqual(restored.get_job(job_id)["item_states"], ["completed"])
        restarted_executor.run_job(job_id)
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

    def test_only_explicit_post_adapter_and_internal_service_call_release_primitive(self):
        root = Path(__file__).parents[1]
        call_sites = []
        for path in [root / "app.py", *(root / "vod_dashboard").glob("*.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))

            class ReleaseCallVisitor(ast.NodeVisitor):
                def __init__(self):
                    self.functions = []

                def visit_FunctionDef(self, node):
                    self.functions.append(node.name)
                    self.generic_visit(node)
                    self.functions.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr
                        == "release_auto_youtube_job_for_execution"
                    ):
                        call_sites.append(
                            (path.name, self.functions[-1] if self.functions else "")
                        )
                    self.generic_visit(node)

            ReleaseCallVisitor().visit(tree)

        self.assertCountEqual(call_sites, [
            ("app.py", "api_release_auto_youtube_job"),
            (
                "auto_youtube_execute.py",
                "release_auto_youtube_job_for_execution",
            ),
            (
                "auto_youtube_execute.py",
                "release_automatic_jobs_for_execution",
            ),
            (
                "auto_youtube_execute.py",
                "recover_uncertain_part_for_execution",
            ),
        ])


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
