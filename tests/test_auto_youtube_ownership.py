from pathlib import Path
import tempfile
import unittest

from vod_dashboard.auto_youtube_ownership import (
    AutoYouTubeOwnedMediaError,
    AutoYouTubeOwnershipUnavailable,
    load_ownership_records,
    ownership_for_local_media,
    require_manual_upload_eligible,
)
from vod_dashboard.media import MediaPathPolicy


class AutoYouTubeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.path = self.media_root / "cptmary" / "vod.mp4"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"media")
        self.policy = MediaPathPolicy(self.media_root)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, state, *, path="cptmary/vod.mp4", parts=None):
        return {
            "streamer": "cptmary",
            "twitch_vod_id": "2858027398",
            "media_path": path,
            "size_bytes": 5,
            "state": state,
            "parts": list(parts or []),
        }

    def ownership(self, state, *, parts=None):
        return ownership_for_local_media(
            self.path,
            streamer="CptMary",
            twitch_vod_id="v2858027398",
            records=[self.record(state, parts=parts)],
            media_policy=self.policy,
        )

    def test_owned_lifecycle_states_block_manual_upload(self):
        for state in (
            "intent_pending",
            "plan_ready",
            "parts_ready",
            "upload_queued",
            "video_confirmed",
            "playlist_pending",
            "completed",
            "blocked_youtube",
            "needs_attention",
        ):
            with self.subTest(state=state):
                ownership = self.ownership(state)
                self.assertTrue(ownership.managed)
                with self.assertRaises(AutoYouTubeOwnedMediaError):
                    require_manual_upload_eligible(
                        self.path,
                        streamer="cptmary",
                        twitch_vod_id="2858027398",
                        records=[self.record(state)],
                        media_policy=self.policy,
                    )

    def test_confirmed_and_pending_statuses_are_distinct(self):
        self.assertEqual(self.ownership("plan_ready").status, "Managed by Auto YouTube")
        self.assertEqual(
            self.ownership("playlist_pending").status,
            "Uploaded by Auto YouTube",
        )
        confirmed_part = self.ownership(
            "needs_attention",
            parts=[
                {
                    "upload_state": "video_confirmed",
                    "youtube_video_id": "video-1",
                }
            ],
        )
        self.assertTrue(confirmed_part.video_confirmed)

    def test_requires_exact_vod_and_media_not_similar_display_metadata(self):
        other_path = self.media_root / "cptmary" / "other.mp4"
        other_path.write_bytes(b"other")
        for path, vod_id, streamer in (
            (other_path, "2858027398", "cptmary"),
            (self.path, "2858027399", "cptmary"),
            (self.path, "2858027398", "different"),
        ):
            with self.subTest(path=path, vod_id=vod_id, streamer=streamer):
                ownership = ownership_for_local_media(
                    path,
                    streamer=streamer,
                    twitch_vod_id=vod_id,
                    records=[self.record("upload_queued")],
                    media_policy=self.policy,
                )
                self.assertFalse(ownership.managed)

    def test_cancelled_is_not_claimed_but_exact_path_survives_missing_sidecar_id(self):
        self.assertFalse(self.ownership("cancelled").managed)
        ownership = ownership_for_local_media(
            self.path,
            streamer="cptmary",
            twitch_vod_id="",
            records=[self.record("upload_queued")],
            media_policy=self.policy,
        )
        self.assertTrue(ownership.managed)

    def test_ledger_load_failure_is_explicit_and_safe(self):
        class BrokenStore:
            def list_records(self):
                raise RuntimeError("private path")

        with self.assertRaises(AutoYouTubeOwnershipUnavailable) as caught:
            load_ownership_records(BrokenStore())
        self.assertEqual(
            str(caught.exception),
            "Auto YouTube ownership could not be verified.",
        )


if __name__ == "__main__":
    unittest.main()
