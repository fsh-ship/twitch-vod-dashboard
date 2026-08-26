from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_multipart as multipart
from vod_dashboard import auto_youtube_prepare as preparation
from vod_dashboard import auto_youtube_replan as replan
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    MAX_AUTOMATIC_REPLANS,
    YouTubeUploadStatePersistenceError,
    YouTubeUploadStateStore,
)


VOD_ID = "2855270041"


class AutoYouTubeReplanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.source = self.media_root / "bearlychen" / "vod.mkv"
        self.source.parent.mkdir()
        self.source.write_bytes(b"original")
        self.source.with_suffix(".info.json").write_text(
            json.dumps({"id": VOD_ID}), encoding="utf-8"
        )
        self.policy = MediaPathPolicy(self.media_root)
        self.streams = (
            multipart.StreamDescriptor("video", "h264", 1920, 1080),
            multipart.StreamDescriptor("audio", "aac", sample_rate=48000, channels=2),
        )
        self.source_probe = multipart.MediaProbeResult(
            43_200.0, self.streams, multipart.stream_signature(self.streams)
        )
        self._store_number = 0
        self.reset_record()

    def tearDown(self):
        self.temp.cleanup()

    def _part(self, generation: str, *, state="ready", attempts=0, video_id=None):
        return {
            "index": 1,
            "media_path": f".auto-youtube/bearlychen/{VOD_ID}/{generation}/parts/part-001-of-002.mkv",
            "size_bytes": 5,
            "duration_seconds": 21_600.0,
            "source_kind": "generated",
            "upload_item_id": "upload-item-1" if state != "ready" else None,
            "upload_state": state,
            "attempts": attempts,
            "youtube_video_id": video_id,
            "playlist_state": "pending",
            "reason": None,
        }

    def reset_record(self, *, part_count=2, replan_count=0, part_state=None, video_id=None):
        self._store_number += 1
        self.store = YouTubeUploadStateStore(
            self.root / f"youtube-upload-state-{self._store_number}.json"
        )
        record, _ = self.store.create_intent_if_absent(
            "bearlychen", VOD_ID,
            source_download_job_id="12",
            source_download_item_id="12-item-1",
            media_path="bearlychen/vod.mkv",
            size_bytes=self.source.stat().st_size,
            playlist_id="PL1",
            plan_inputs={
                "title_template": "{title}", "description_template": "",
                "description_fallback": "", "privacy_status": "private",
                "category_id": "20", "tags": [],
            },
        )
        record = self.store.set_upload_plan("bearlychen", VOD_ID, {
            "title": "Frozen", "description": "Frozen description",
            "privacy_status": "private", "category_id": "20", "tags": [],
        })
        planning_record = dict(
            record, source_duration_seconds=self.source_probe.duration_seconds
        )
        plan = replan._multipart_plan(
            planning_record, self.source_probe, part_count
        )
        current_generation = preparation.generation_id(record, plan)
        split = {
            "mode": "stream_copy",
            "generation_id": current_generation,
            "target_duration_seconds": multipart.TARGET_DURATION_SECONDS,
            "target_size_bytes": multipart.TARGET_SIZE_BYTES,
            "split_points_seconds": list(plan.split_points_seconds),
            "replan_count": replan_count,
        }
        parts = [] if part_state is None else [
            self._part(
                current_generation, state=part_state,
                attempts=7, video_id=video_id,
            )
        ]
        self.record = self.store.set_preparation(
            "bearlychen", VOD_ID,
            source_duration_seconds=self.source_probe.duration_seconds,
            state="needs_attention", split=split, parts=parts,
            reason="multipart_replan_required",
        )
        self.generation_root = replan.authorized_generation_root(
            self.record, self.policy
        )
        self.generation_root.mkdir(parents=True, exist_ok=True)
        (self.generation_root / "invalid-part.mkv").write_bytes(b"invalid")
        return self.record

    def service(self, **changes):
        values = {
            "state_store": self.store,
            "media_policy": self.policy,
            "probe": mock.Mock(return_value=self.source_probe),
        }
        values.update(changes)
        return replan.AutoYouTubeReplanService(**values)

    def test_two_to_three_is_deterministic_and_preserves_frozen_ownership(self):
        before = self.store.get("bearlychen", VOD_ID)
        old_generation = before["split"]["generation_id"]
        unrelated_generation = self.generation_root.parent / "unrelated-generation"
        unrelated_generation.mkdir(); (unrelated_generation / "keep").write_bytes(b"keep")
        other_vod = self.media_root / ".auto-youtube" / "other" / "999999" / "generation"
        other_vod.mkdir(parents=True); (other_vod / "keep").write_bytes(b"keep")

        result = self.service().reconcile()

        self.assertEqual(result["replanned"], 1)
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["state"], "parts_preparing")
        self.assertIsNone(saved["reason"])
        self.assertEqual(saved["split"]["replan_count"], 1)
        self.assertEqual(
            saved["split"]["split_points_seconds"],
            list(multipart.deterministic_split_points(43_200.0, 3)),
        )
        self.assertNotEqual(saved["split"]["generation_id"], old_generation)
        for name in (
            "streamer", "twitch_vod_id", "source_download_job_id",
            "source_download_item_id", "media_path", "size_bytes",
            "source_duration_seconds", "playlist_id", "plan_inputs", "upload_plan",
        ):
            self.assertEqual(saved[name], before[name], name)
        self.assertFalse(self.generation_root.exists())
        self.assertTrue(unrelated_generation.exists())
        self.assertTrue(other_vod.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(self.service().reconcile()["ignored"], 1)
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["split"]["replan_count"], 1
        )

    def test_three_to_four_increments_once(self):
        self.reset_record(part_count=3, replan_count=1)
        self.assertEqual(self.service().replan_record(self.record), "replanned")
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(len(saved["split"]["split_points_seconds"]) + 1, 4)
        self.assertEqual(saved["split"]["replan_count"], 2)

    def test_exactly_three_replans_are_allowed_then_exhausted(self):
        self.reset_record(part_count=2, replan_count=0)
        for expected_count in range(1, MAX_AUTOMATIC_REPLANS + 1):
            current = self.store.get("bearlychen", VOD_ID)
            self.assertEqual(self.service().replan_record(current), "replanned")
            saved = self.store.get("bearlychen", VOD_ID)
            self.assertEqual(saved["split"]["replan_count"], expected_count)
            self.store.update_record(
                "bearlychen", VOD_ID, state="needs_attention",
                reason="multipart_replan_required",
            )
        exhausted = self.store.get("bearlychen", VOD_ID)
        exhausted_root = replan.authorized_generation_root(exhausted, self.policy)
        exhausted_root.mkdir(parents=True)
        generation = exhausted["split"]["generation_id"]

        self.assertEqual(self.service().replan_record(exhausted), "exhausted")

        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["reason"], "multipart_replan_exhausted")
        self.assertEqual(saved["split"]["replan_count"], MAX_AUTOMATIC_REPLANS)
        self.assertEqual(saved["split"]["generation_id"], generation)
        self.assertTrue(exhausted_root.exists())
        self.assertEqual(self.service().reconcile()["ignored"], 1)

    def test_crash_before_cleanup_retries_the_same_transition(self):
        before = self.store.get("bearlychen", VOD_ID)
        expected = self.service()._replacement(before, self.source_probe)[1]
        crashing = self.service(rmtree=mock.Mock(side_effect=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            crashing.replan_record(before)
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["split"], before["split"])
        self.assertTrue(self.generation_root.exists())

        self.assertEqual(self.service().replan_record(before), "replanned")
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["split"]["generation_id"],
            expected["generation_id"],
        )

    def test_cleanup_before_persistence_retries_the_same_transition(self):
        before = self.store.get("bearlychen", VOD_ID)
        expected = self.service()._replacement(before, self.source_probe)[1]
        with mock.patch.object(
            self.store, "replace_split_for_replan",
            side_effect=YouTubeUploadStatePersistenceError("full"),
        ):
            self.assertEqual(self.service().replan_record(before), "pending")
        self.assertFalse(self.generation_root.exists())
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["split"], before["split"])

        self.assertEqual(self.service().replan_record(before), "replanned")
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["split"]["generation_id"],
            expected["generation_id"],
        )

    def test_cleanup_failure_never_mutates_the_plan_or_original(self):
        before = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(
            self.service(rmtree=mock.Mock(side_effect=OSError("denied"))).replan_record(before),
            "attention",
        )
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["split"], before["split"])
        self.assertEqual(saved["reason"], "multipart_replan_failed")
        self.assertTrue(self.generation_root.exists())
        self.assertTrue(self.source.exists())

    def test_missing_changed_or_duration_mismatched_source_blocks_before_cleanup(self):
        cases = ("missing", "changed", "duration")
        for case in cases:
            with self.subTest(case=case):
                if case != "missing":
                    self.source.write_bytes(b"original")
                self.reset_record()
                if case == "missing":
                    self.source.unlink()
                elif case == "changed":
                    self.source.write_bytes(b"changed-size")
                else:
                    bad_probe = multipart.MediaProbeResult(
                        43_190.0, self.streams,
                        multipart.stream_signature(self.streams),
                    )
                service = self.service(**(
                    {"probe": mock.Mock(return_value=bad_probe)}
                    if case == "duration" else {}
                ))
                self.assertEqual(service.replan_record(self.record), "attention")
                self.assertEqual(
                    self.store.get("bearlychen", VOD_ID)["reason"],
                    "multipart_replan_source_invalid",
                )
                self.assertTrue(self.generation_root.exists())

    def test_corrupt_plan_traversal_and_link_redirection_fail_closed(self):
        for case in ("points", "traversal", "link"):
            with self.subTest(case=case):
                self.reset_record()
                changed = dict(self.record)
                if case == "points":
                    changed["split"] = dict(
                        changed["split"], split_points_seconds=[20_000.0]
                    )
                    service = self.service()
                elif case == "traversal":
                    changed["split"] = dict(
                        changed["split"], generation_id="../escape"
                    )
                    service = self.service()
                else:
                    unrelated = self.media_root / ".auto-youtube" / "unrelated"
                    unrelated.mkdir(parents=True)
                    real_policy = MediaPathPolicy(self.media_root)
                    def redirected(raw, **kwargs):
                        if str(raw).startswith(".auto-youtube"):
                            return unrelated.resolve()
                        return real_policy.resolve_media_path(raw, **kwargs)
                    policy = mock.Mock()
                    policy.media_root = self.media_root
                    policy.resolve_media_path.side_effect = redirected
                    service = replan.AutoYouTubeReplanService(
                        state_store=self.store, media_policy=policy,
                        probe=mock.Mock(return_value=self.source_probe),
                        source_validator=lambda record, media_policy: self.source,
                    )
                    self.assertEqual(service.replan_record(changed), "attention")
                    self.assertTrue(unrelated.exists())
                    self.assertTrue(self.generation_root.exists())
                    continue
                self.assertEqual(service.replan_record(changed), "attention")
                self.assertTrue(self.generation_root.exists())
                self.assertEqual(
                    self.store.get("bearlychen", VOD_ID)["reason"],
                    "multipart_replan_unsafe",
                )

    def test_remote_part_states_and_upload_job_never_delete_or_replan(self):
        cases = (
            ("transfer_started", None),
            ("video_confirmed", "YT123"),
            ("uncertain", None),
            ("ready", "YT123"),
        )
        for part_state, video_id in cases:
            with self.subTest(part_state=part_state, video_id=video_id):
                self.reset_record(part_state=part_state, video_id=video_id)
                self.assertEqual(self.service().replan_record(self.record), "attention")
                saved = self.store.get("bearlychen", VOD_ID)
                self.assertEqual(saved["reason"], "multipart_replan_unsafe")
                self.assertEqual(saved["parts"][0]["attempts"], 7)
                self.assertTrue(self.generation_root.exists())

        self.reset_record()
        with_job = self.store.update_record(
            "bearlychen", VOD_ID, upload_job_id="99"
        )
        self.assertEqual(self.service().replan_record(with_job), "attention")
        self.assertTrue(self.generation_root.exists())

    def test_stale_snapshot_cannot_delete_after_durable_state_changes(self):
        stale = self.store.get("bearlychen", VOD_ID)
        self.store.update_record("bearlychen", VOD_ID, upload_job_id="99")

        self.assertEqual(self.service().replan_record(stale), "ignored")

        self.assertTrue(self.generation_root.exists())
        self.assertEqual(
            self.store.get("bearlychen", VOD_ID)["upload_job_id"], "99"
        )

    def test_parts_ready_and_upload_queued_are_ignored_without_cleanup(self):
        for state in ("parts_ready", "upload_queued"):
            with self.subTest(state=state):
                self.reset_record(part_state="ready")
                changed = self.store.update_record(
                    "bearlychen", VOD_ID, state=state,
                    **({"upload_job_id": "99"} if state == "upload_queued" else {}),
                )
                self.assertEqual(self.service().replan_record(changed), "ignored")
                self.assertTrue(self.generation_root.exists())

    def test_service_has_no_ffmpeg_job_youtube_or_worker_dependency(self):
        service = self.service()
        self.assertFalse(any(
            hasattr(service, name)
            for name in ("ffmpeg", "job_manager", "youtube", "worker")
        ))
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
