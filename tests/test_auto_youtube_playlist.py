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
from vod_dashboard.auto_youtube_playlist import (
    AutoYouTubePlaylistError,
    AutoYouTubePlaylistService,
    playlist_contains_video,
)
from vod_dashboard.job_store import JobStore
from vod_dashboard.jobs import JobManager
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStatePersistenceError,
    YouTubeUploadStateStore,
)


class AutoYouTubePlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.store = YouTubeUploadStateStore(
            self.root / "youtube-upload-state.json"
        )
        self.manager = JobManager(
            job_store=JobStore(self.root / "jobs.json"),
            media_root=self.media_root,
        )
        self.settings = {"youtube_playlist_id": "CURRENT_SETTINGS_PLAYLIST"}

    def tearDown(self):
        self.temp.cleanup()

    def _probe(self, _path: Path) -> MediaProbeResult:
        return MediaProbeResult(1.0, (), ())

    def _create_confirmed_bundle(
        self,
        total=1,
        *,
        streamer="cptmary",
        vod_id="2857167152",
        playlist_chainer=None,
    ):
        source = self.media_root / streamer / "source.mkv"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source-media")
        source.with_suffix(".info.json").write_text(
            json.dumps({"id": vod_id}), encoding="utf-8"
        )
        self.store.create_intent_if_absent(
            streamer,
            vod_id,
            source_download_job_id="12",
            source_download_item_id="12-item-1",
            media_path=f"{streamer}/source.mkv",
            size_bytes=source.stat().st_size,
            playlist_id="FROZEN_PLAYLIST",
            plan_inputs={
                "title_template": "{title}",
                "description_template": "{title}",
                "description_fallback": "",
                "privacy_status": "unlisted",
                "category_id": "20",
                "tags": ["frozen"],
            },
        )
        self.store.set_upload_plan(
            streamer,
            vod_id,
            {
                "title": "Frozen title",
                "description": "Frozen description",
                "privacy_status": "unlisted",
                "category_id": "20",
                "tags": ["frozen"],
            },
        )
        if total == 1:
            parts = [{
                "index": 1,
                "media_path": f"{streamer}/source.mkv",
                "size_bytes": source.stat().st_size,
                "duration_seconds": 1.0,
                "source_kind": "original",
                "upload_item_id": None,
                "upload_state": "ready",
                "attempts": 0,
                "youtube_video_id": None,
                "playlist_state": "pending",
                "reason": None,
            }]
            split = None
        else:
            parts = []
            for index in range(1, total + 1):
                relative = (
                    f".auto-youtube/{streamer}/{vod_id}/g1/parts/"
                    f"part-{index:03d}.mkv"
                )
                part_path = self.media_root / relative
                part_path.parent.mkdir(parents=True, exist_ok=True)
                part_path.write_bytes(f"part-{index}".encode())
                parts.append({
                    "index": index,
                    "media_path": relative,
                    "size_bytes": part_path.stat().st_size,
                    "duration_seconds": 1.0,
                    "source_kind": "generated",
                    "upload_item_id": None,
                    "upload_state": "ready",
                    "attempts": 0,
                    "youtube_video_id": None,
                    "playlist_state": "pending",
                    "reason": None,
                })
            split = {
                "mode": "stream_copy",
                "generation_id": "g1",
                "target_duration_seconds": 42300,
                "target_size_bytes": 250000000000,
                "split_points_seconds": [float(index) for index in range(1, total)],
            }
        self.store.set_preparation(
            streamer,
            vod_id,
            source_duration_seconds=float(total),
            state="parts_ready",
            split=split,
            parts=parts,
        )
        materializer = AutoYouTubeMaterializationService(
            state_store=self.store,
            job_manager=self.manager,
            media_policy=MediaPathPolicy(self.media_root),
            probe=self._probe,
        )
        self.assertEqual(materializer.reconcile()["queued"], 1)
        job_id = self.store.get(streamer, vod_id)["upload_job_id"]
        video_sender = mock.Mock(
            side_effect=[f"VIDEO_{index}" for index in range(1, total + 1)]
        )
        executor = AutoYouTubeExecutionService(
            state_store=self.store,
            job_manager=self.manager,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=mock.Mock(return_value=mock.Mock()),
            request_builder=mock.Mock(return_value={"request": "video"}),
            request_sender=video_sender,
            probe=self._probe,
            playlist_chainer=playlist_chainer,
        )
        self.assertTrue(executor.release_auto_youtube_job_for_execution(job_id))
        executor.run_job(job_id)
        if playlist_chainer is None:
            self.assertEqual(
                self.store.get(streamer, vod_id)["state"], "playlist_pending"
            )
        return job_id, streamer, vod_id, video_sender

    def _service(self, *, membership, inserter, manager=None):
        return AutoYouTubePlaylistService(
            state_store=self.store,
            job_manager=manager or self.manager,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=mock.Mock(return_value=mock.Mock()),
            membership_lookup=membership,
            playlist_inserter=inserter,
        )

    def _automatic_playlist_chainer(self, *, membership, inserter):
        service = self._service(membership=membership, inserter=inserter)
        return mock.Mock(side_effect=service.add_to_playlist)

    def test_membership_lookup_paginates_and_matches_the_exact_video_id(self):
        first = mock.Mock()
        first.execute.return_value = {
            "items": [{
                "snippet": {"resourceId": {"videoId": "VIDEO_1_extra"}}
            }],
            "nextPageToken": "next-page",
        }
        second = mock.Mock()
        second.execute.return_value = {
            "items": [{
                "snippet": {"resourceId": {"videoId": "VIDEO_1"}}
            }]
        }
        service = mock.Mock()
        service.playlistItems.return_value.list.side_effect = [first, second]

        self.assertTrue(
            playlist_contains_video(service, "FROZEN_PLAYLIST", "VIDEO_1")
        )
        self.assertEqual(service.playlistItems.return_value.list.call_count, 2)
        self.assertEqual(
            service.playlistItems.return_value.list.call_args_list[0].kwargs,
            {
                "part": "snippet",
                "playlistId": "FROZEN_PLAYLIST",
                "maxResults": 50,
            },
        )
        self.assertEqual(
            service.playlistItems.return_value.list.call_args_list[1].kwargs,
            {
                "part": "snippet",
                "playlistId": "FROZEN_PLAYLIST",
                "maxResults": 50,
                "pageToken": "next-page",
            },
        )

    def test_job_79_playlist_action_confirms_frozen_membership_without_video_upload(self):
        self.manager.counter = 78
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        self.assertEqual(job_id, "79")
        membership = mock.Mock(return_value=False)
        inserter = mock.Mock(return_value={"id": "PLAYLIST_ITEM_79"})

        result = self._service(
            membership=membership, inserter=inserter
        ).add_to_playlist(job_id)

        record = YouTubeUploadStateStore(self.store.path).get(streamer, vod_id)
        self.assertEqual(result, {"status": "completed", "completed": True})
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["youtube_video_id"], "VIDEO_1")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        membership.assert_called_once_with(mock.ANY, "FROZEN_PLAYLIST", "VIDEO_1")
        inserter.assert_called_once_with(mock.ANY, "FROZEN_PLAYLIST", "VIDEO_1")
        video_sender.assert_called_once()
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

    def test_successful_video_upload_automatically_confirms_playlist_membership(self):
        membership = mock.Mock(return_value=False)
        inserter = mock.Mock(return_value={"id": "PLAYLIST_ITEM_1"})
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            playlist_chainer=chainer
        )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["youtube_video_id"], "VIDEO_1")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        chainer.assert_called_once_with(job_id)
        membership.assert_called_once_with(mock.ANY, "FROZEN_PLAYLIST", "VIDEO_1")
        inserter.assert_called_once_with(mock.ANY, "FROZEN_PLAYLIST", "VIDEO_1")
        video_sender.assert_called_once()
        self.assertTrue((self.media_root / streamer / "source.mkv").exists())
        self.assertEqual(len(self.manager.snapshot_jobs()), 1)

    def test_automatic_playlist_chain_skips_insert_when_video_is_already_present(self):
        membership = mock.Mock(return_value=True)
        inserter = mock.Mock()
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            playlist_chainer=chainer
        )

        self.assertEqual(self.store.get(streamer, vod_id)["state"], "completed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        chainer.assert_called_once_with(job_id)
        membership.assert_called_once()
        inserter.assert_not_called()
        video_sender.assert_called_once()

    def test_automatic_playlist_chain_keeps_confirmed_video_when_preinsert_persistence_fails(self):
        membership = mock.Mock(return_value=False)
        inserter = mock.Mock()
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )
        with mock.patch.object(
            self.store,
            "begin_part_playlist_insertion",
            side_effect=YouTubeUploadStatePersistenceError("full"),
        ):
            job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
                playlist_chainer=chainer
            )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["youtube_video_id"], "VIDEO_1")
        self.assertEqual(record["parts"][0]["playlist_state"], "pending")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        chainer.assert_called_once_with(job_id)
        inserter.assert_not_called()
        video_sender.assert_called_once()

    def test_automatic_playlist_chain_handles_ambiguous_insert_without_reupload(self):
        membership = mock.Mock(side_effect=[False, False])
        inserter = mock.Mock(side_effect=RuntimeError("connection lost"))
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            playlist_chainer=chainer
        )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(record["parts"][0]["upload_state"], "video_confirmed")
        self.assertEqual(record["parts"][0]["playlist_state"], "uncertain")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        chainer.assert_called_once_with(job_id)
        self.assertEqual(membership.call_count, 2)
        inserter.assert_called_once()
        video_sender.assert_called_once()

    def test_automatic_playlist_chain_confirms_ambiguous_insert_after_membership_recheck(self):
        membership = mock.Mock(side_effect=[False, True])
        inserter = mock.Mock(side_effect=RuntimeError("connection lost"))
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            playlist_chainer=chainer
        )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        self.assertEqual(membership.call_count, 2)
        inserter.assert_called_once()
        video_sender.assert_called_once()

    def test_automatic_multipart_playlist_chain_confirms_serially_and_skips_present_part(self):
        membership = mock.Mock(side_effect=[True, False, False])
        inserter = mock.Mock(side_effect=[
            {"id": "PLAYLIST_ITEM_2"}, {"id": "PLAYLIST_ITEM_3"}
        ])
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            3, playlist_chainer=chainer
        )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(
            [part["playlist_state"] for part in record["parts"]],
            ["confirmed", "confirmed", "confirmed"],
        )
        self.assertEqual(self.manager.get_job(job_id)["item_states"], [
            "completed", "completed", "completed"
        ])
        chainer.assert_called_once_with(job_id)
        self.assertEqual(inserter.call_count, 2)
        self.assertEqual(video_sender.call_count, 3)

    def test_automatic_multipart_playlist_chain_is_serial_and_stops_after_ambiguity(self):
        membership = mock.Mock(side_effect=[False, False, False])
        inserter = mock.Mock(side_effect=[
            {"id": "PLAYLIST_ITEM_1"}, RuntimeError("connection lost")
        ])
        chainer = self._automatic_playlist_chainer(
            membership=membership, inserter=inserter
        )

        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(
            3, playlist_chainer=chainer
        )

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(
            [part["playlist_state"] for part in record["parts"]],
            ["confirmed", "uncertain", "pending"],
        )
        self.assertEqual(self.manager.get_job(job_id)["item_states"], [
            "completed", "completed", "completed"
        ])
        chainer.assert_called_once_with(job_id)
        self.assertEqual(inserter.call_count, 2)
        self.assertEqual(video_sender.call_count, 3)

    def test_job_79_recovery_completes_deferred_gate_then_accepts_playlist_action(self):
        self.manager.counter = 78
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        self.assertEqual(job_id, "79")
        item_id = self.manager.get_job(job_id)["item_ids"][0]
        self.assertTrue(
            self.manager.reset_auto_youtube_item_to_queued(job_id, item_id)
        )
        self.assertTrue(self.manager.defer_auto_youtube_job(job_id))
        stale = self.manager.get_job(job_id)
        self.assertEqual(stale["item_states"], ["queued"])
        self.assertTrue(stale["execution_deferred"])

        restarted = JobManager(
            job_store=JobStore(self.root / "jobs.json"),
            media_root=self.media_root,
        )
        restarted.restore_from_store()
        recovery_sender = mock.Mock()
        executor = AutoYouTubeExecutionService(
            state_store=self.store,
            job_manager=restarted,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=mock.Mock(),
            request_builder=mock.Mock(),
            request_sender=recovery_sender,
            probe=self._probe,
        )

        self.assertEqual(executor.reconcile()["confirmed"], 1)
        recovered = restarted.get_job(job_id)
        self.assertEqual(recovered["item_states"], ["completed"])
        self.assertFalse(recovered["execution_deferred"])
        self.assertIsNone(restarted.claim_next_item(job_id))
        self.assertEqual(
            self.store.get(streamer, vod_id)["state"], "playlist_pending"
        )
        self.assertEqual(
            self.store.get(streamer, vod_id)["parts"][0]["youtube_video_id"],
            "VIDEO_1",
        )
        before_repeat = restarted.get_job(job_id)
        self.assertEqual(executor.reconcile()["confirmed"], 1)
        self.assertEqual(restarted.get_job(job_id), before_repeat)
        recovery_sender.assert_not_called()
        video_sender.assert_called_once()
        self.assertEqual(len(restarted.snapshot_jobs()), 1)

        reloaded = JobManager(
            job_store=JobStore(self.root / "jobs.json"),
            media_root=self.media_root,
        )
        reloaded.restore_from_store()
        self.assertEqual(reloaded.get_job(job_id)["item_states"], ["completed"])
        self.assertFalse(reloaded.get_job(job_id)["execution_deferred"])
        with self.assertRaisesRegex(
            AutoYouTubeExecutionError, "release_not_allowed"
        ):
            executor = AutoYouTubeExecutionService(
                state_store=self.store,
                job_manager=reloaded,
                media_policy=MediaPathPolicy(self.media_root),
                settings_provider=lambda: dict(self.settings),
                service_getter=mock.Mock(),
                request_builder=mock.Mock(),
                request_sender=mock.Mock(),
                probe=self._probe,
            )
            executor.release_auto_youtube_job_for_execution(job_id)

        membership = mock.Mock(return_value=True)
        inserter = mock.Mock()
        result = self._service(
            manager=reloaded, membership=membership, inserter=inserter
        ).add_to_playlist(job_id)
        record = self.store.get(streamer, vod_id)
        self.assertEqual(result, {"status": "completed", "completed": True})
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        inserter.assert_not_called()
        membership.assert_called_once()

    def test_recovery_clears_a_stale_deferred_gate_from_an_already_completed_job(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        self.assertTrue(self.manager.defer_auto_youtube_job(job_id))
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])
        self.assertTrue(self.manager.get_job(job_id)["execution_deferred"])

        restarted = JobManager(
            job_store=JobStore(self.root / "jobs.json"),
            media_root=self.media_root,
        )
        restarted.restore_from_store()
        recovery_sender = mock.Mock()
        executor = AutoYouTubeExecutionService(
            state_store=self.store,
            job_manager=restarted,
            media_policy=MediaPathPolicy(self.media_root),
            settings_provider=lambda: dict(self.settings),
            service_getter=mock.Mock(),
            request_builder=mock.Mock(),
            request_sender=recovery_sender,
            probe=self._probe,
        )

        self.assertEqual(executor.reconcile()["confirmed"], 1)
        self.assertEqual(restarted.get_job(job_id)["item_states"], ["completed"])
        self.assertFalse(restarted.get_job(job_id)["execution_deferred"])
        self.assertEqual(
            self.store.get(streamer, vod_id)["state"], "playlist_pending"
        )
        recovery_sender.assert_not_called()
        video_sender.assert_called_once()

    def test_existing_membership_is_confirmed_without_insert_and_is_idempotent(self):
        job_id, _streamer, _vod_id, video_sender = self._create_confirmed_bundle()
        membership = mock.Mock(return_value=True)
        inserter = mock.Mock()
        service = self._service(membership=membership, inserter=inserter)

        self.assertTrue(service.add_to_playlist(job_id)["completed"])
        self.assertEqual(
            service.add_to_playlist(job_id),
            {"status": "already_confirmed", "completed": True},
        )
        inserter.assert_not_called()
        membership.assert_called_once()
        video_sender.assert_called_once()

    def test_preinsert_persistence_failure_sends_no_playlist_mutation(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        membership = mock.Mock(return_value=False)
        inserter = mock.Mock()
        service = self._service(membership=membership, inserter=inserter)
        with mock.patch.object(
            self.store,
            "begin_part_playlist_insertion",
            side_effect=YouTubeUploadStatePersistenceError("full"),
        ):
            with self.assertRaisesRegex(
                AutoYouTubePlaylistError, "playlist_persistence_failed"
            ):
                service.add_to_playlist(job_id)

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "playlist_pending")
        self.assertEqual(record["parts"][0]["playlist_state"], "pending")
        inserter.assert_not_called()
        video_sender.assert_called_once()

    def test_ambiguous_insert_confirms_only_when_readback_proves_membership(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        membership = mock.Mock(side_effect=[False, True])
        inserter = mock.Mock(side_effect=RuntimeError("connection lost"))

        result = self._service(
            membership=membership, inserter=inserter
        ).add_to_playlist(job_id)

        record = self.store.get(streamer, vod_id)
        self.assertTrue(result["completed"])
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["parts"][0]["playlist_state"], "confirmed")
        inserter.assert_called_once()
        self.assertEqual(membership.call_count, 2)
        video_sender.assert_called_once()

    def test_ambiguous_insert_becomes_attention_without_blind_retry(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle()
        membership = mock.Mock(return_value=False)
        inserter = mock.Mock(side_effect=RuntimeError("connection lost"))
        service = self._service(membership=membership, inserter=inserter)

        with self.assertRaisesRegex(AutoYouTubePlaylistError, "needs_attention"):
            service.add_to_playlist(job_id)
        with self.assertRaisesRegex(AutoYouTubePlaylistError, "needs_attention"):
            service.add_to_playlist(job_id)

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(record["parts"][0]["playlist_state"], "uncertain")
        inserter.assert_called_once()
        video_sender.assert_called_once()

    def test_multipart_playlist_action_is_serial_and_preserves_confirmed_parts(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(3)
        membership = mock.Mock(side_effect=[True, False, False])
        inserter = mock.Mock(side_effect=[{"id": "ITEM_2"}, {"id": "ITEM_3"}])

        result = self._service(
            membership=membership, inserter=inserter
        ).add_to_playlist(job_id)

        record = self.store.get(streamer, vod_id)
        self.assertTrue(result["completed"])
        self.assertEqual(record["state"], "completed")
        self.assertEqual(
            [part["playlist_state"] for part in record["parts"]],
            ["confirmed", "confirmed", "confirmed"],
        )
        self.assertEqual(inserter.call_count, 2)
        self.assertEqual(video_sender.call_count, 3)

    def test_multipart_ambiguous_part_stops_later_playlist_inserts(self):
        job_id, streamer, vod_id, video_sender = self._create_confirmed_bundle(3)
        membership = mock.Mock(side_effect=[False, False, False])
        inserter = mock.Mock(side_effect=[{"id": "ITEM_1"}, RuntimeError("lost")])

        with self.assertRaisesRegex(AutoYouTubePlaylistError, "needs_attention"):
            self._service(membership=membership, inserter=inserter).add_to_playlist(job_id)

        record = self.store.get(streamer, vod_id)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(
            [part["playlist_state"] for part in record["parts"]],
            ["confirmed", "uncertain", "pending"],
        )
        self.assertEqual(inserter.call_count, 2)
        self.assertEqual(video_sender.call_count, 3)

    def test_missing_video_confirmation_refuses_playlist_mutation(self):
        job_id, streamer, vod_id, _video_sender = self._create_confirmed_bundle(2)
        state = self.store.load()
        record = state["uploads"][f"{streamer}:{vod_id}"]
        record["parts"][1]["upload_state"] = "queued"
        record["parts"][1]["youtube_video_id"] = None
        self.store.replace_state(state)
        membership = mock.Mock()
        inserter = mock.Mock()

        with self.assertRaisesRegex(AutoYouTubePlaylistError, "playlist_not_pending"):
            self._service(membership=membership, inserter=inserter).add_to_playlist(job_id)

        membership.assert_not_called()
        inserter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
