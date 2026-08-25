from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from vod_dashboard import youtube_upload_state as upload_state


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class YouTubeUploadStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.temp.name)
        self.path = self.dashboard_dir / upload_state.YOUTUBE_UPLOAD_STATE_FILE_NAME
        self.store = upload_state.YouTubeUploadStateStore(
            self.path, clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _record(**updates):
        record = {
            "streamer": "bearlychen",
            "twitch_vod_id": "2855270041",
            "source_download_job_id": "38",
            "source_download_item_id": "38-item-1",
            "media_path": "bearlychen/video.mp4",
            "size_bytes": 16_754_671_664,
            "state": "intent_pending",
            "upload_job_id": None,
            "attempts": 0,
            "youtube_video_id": None,
            "playlist_id": None,
            "playlist_state": "not_requested",
            "reason": None,
            "created_at": "2026-08-25T12:00:00Z",
            "updated_at": "2026-08-25T12:00:00Z",
        }
        record.update(updates)
        return record

    def _valid_state(self, **updates):
        state = {
            "version": 1,
            "uploads": {"bearlychen:2855270041": self._record()},
        }
        state.update(updates)
        return state

    def _create(self, **updates):
        values = {
            "streamer": "BearLyChen",
            "twitch_vod_id": "2855270041",
            "source_download_job_id": "38",
            "source_download_item_id": "38-item-1",
            "media_path": "bearlychen/video.mp4",
            "size_bytes": 16_754_671_664,
        }
        values.update(updates)
        return self.store.create_intent_if_absent(**values)

    def test_missing_file_is_a_healthy_empty_v1_store(self):
        self.assertEqual(self.store.load(), upload_state.empty_youtube_upload_state())
        self.assertEqual(self.store.health(), {"healthy": True, "reason": None})
        self.assertEqual(
            upload_state.YouTubeUploadStateStore.from_dashboard_dir(
                self.dashboard_dir
            ).path,
            self.path,
        )

    def test_valid_v1_loads_and_uses_canonical_key(self):
        self.path.write_text(json.dumps(self._valid_state()), encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded["version"], 1)
        self.assertEqual(loaded["uploads"]["bearlychen:2855270041"], self._record())
        self.assertEqual(
            upload_state.canonical_upload_key("@BearLyChen", "2855270041"),
            "bearlychen:2855270041",
        )

    def test_invalid_primary_documents_fail_closed(self):
        cases = {
            "wrong-version": {"version": 2, "uploads": {}},
            "malformed-root": {"version": 1, "records": {}},
            "malformed-record": {"version": 1, "uploads": {"bearlychen:2855270041": {}}},
            "bad-state": self._valid_state(uploads={"bearlychen:2855270041": self._record(state="unknown")}),
            "bad-playlist-state": self._valid_state(uploads={"bearlychen:2855270041": self._record(playlist_state="unknown")}),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(upload_state.YouTubeUploadStateLoadError):
                    self.store.load()
                self.assertFalse(self.store.health()["healthy"])

    def test_create_rejects_unsafe_identity_and_media_metadata(self):
        invalid = (
            {"twitch_vod_id": "v2855270041"},
            {"twitch_vod_id": "not-a-vod"},
            {"streamer": "not valid!"},
            {"media_path": "/srv/" + "vods/video.mp4"},
            {"media_path": "../video.mp4"},
            {"media_path": "C:/vods/video.mp4"},
            {"size_bytes": -1},
            {"size_bytes": True},
        )
        for update in invalid:
            with self.subTest(update=update):
                with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
                    self._create(**update)
        self.assertFalse(self.path.exists())

    def test_create_persists_complete_compact_record_and_no_host_path(self):
        record, created = self._create()
        self.assertTrue(created)
        self.assertEqual(record["state"], "intent_pending")
        self.assertEqual(record["playlist_state"], "not_requested")
        self.assertEqual(record["created_at"], "2026-08-25T12:00:00Z")
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn('"bearlychen:2855270041"', raw)
        self.assertNotIn(str(self.dashboard_dir), raw)
        self.assertNotIn("/srv/" + "vods", raw)
        self.assertEqual(self.store.get("@bearlychen", "2855270041"), record)

    def test_create_is_idempotent_and_never_replaces_existing_owner(self):
        first, created = self._create()
        second, repeated = self._create(
            source_download_job_id="39",
            source_download_item_id="39-item-1",
            media_path="other/video.mp4",
            size_bytes=1,
        )
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(second, first)
        self.assertEqual(self.store.load()["uploads"], {"bearlychen:2855270041": first})

    def test_concurrent_create_if_absent_produces_one_owner(self):
        barrier = threading.Barrier(12)

        def create(index: int):
            barrier.wait()
            return self._create(
                streamer="@BearLyChen" if index % 2 else "bearlychen",
                source_download_job_id=str(38 + index),
                source_download_item_id=f"{38 + index}-item-1",
                media_path=f"bearlychen/video-{index}.mp4",
                size_bytes=index,
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(create, range(12)))
        self.assertEqual(sum(created for _record, created in results), 1)
        self.assertEqual(len(self.store.load()["uploads"]), 1)

    def test_atomic_failure_preserves_previous_primary_and_visible_state(self):
        first, _ = self._create()
        previous = self.path.read_bytes()
        with mock.patch.object(
            upload_state, "atomic_write_text", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaises(upload_state.YouTubeUploadStatePersistenceError):
                self._create(twitch_vod_id="2855270042")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.store.load()["uploads"], {"bearlychen:2855270041": first})

    def test_atomic_writer_failure_cleans_temporary_file_best_effort(self):
        from vod_dashboard import runtime_files

        with mock.patch.object(runtime_files.os, "replace", side_effect=OSError("ENOSPC")):
            with self.assertRaises(upload_state.YouTubeUploadStatePersistenceError):
                self._create()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.dashboard_dir.glob(".youtube-upload-state.json.*.tmp")), [])

    def test_corrupt_primary_is_never_overwritten_automatically(self):
        corrupt = b"{ definitely not JSON"
        self.path.write_bytes(corrupt)
        with mock.patch.object(upload_state, "atomic_write_text") as write:
            with self.assertRaises(upload_state.YouTubeUploadStateLoadError):
                self._create()
        write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), corrupt)

    def test_valid_transitions_and_durable_attachments(self):
        self._create()
        queued = self.store.update_record(
            "bearlychen", "2855270041", upload_job_id="45", state="upload_queued", attempts=1
        )
        started = self.store.update_record(
            "bearlychen", "2855270041", state="transfer_started"
        )
        confirmed = self.store.update_record(
            "bearlychen", "2855270041", youtube_video_id="abc_DEF-123", state="video_confirmed"
        )
        playlist = self.store.update_record(
            "bearlychen", "2855270041", playlist_id="PL123", playlist_state="pending", state="playlist_pending"
        )
        completed = self.store.update_record(
            "bearlychen", "2855270041", playlist_state="inserting"
        )
        completed = self.store.update_record(
            "bearlychen", "2855270041", playlist_state="confirmed", state="completed"
        )
        self.assertEqual(queued["upload_job_id"], "45")
        self.assertEqual(started["state"], "transfer_started")
        self.assertEqual(confirmed["youtube_video_id"], "abc_DEF-123")
        self.assertEqual(playlist["playlist_state"], "pending")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.store.load()["uploads"]["bearlychen:2855270041"], completed)

    def test_invalid_transitions_and_unvalidated_values_are_rejected(self):
        self._create()
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", state="completed")
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", state="unknown")
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", playlist_state="confirmed")
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", reason="raw exception: token=secret")
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", attempts=-1)

    def test_identity_and_confirmed_video_id_are_immutable(self):
        self._create()
        self.store.update_record(
            "bearlychen", "2855270041", upload_job_id="45", state="upload_queued"
        )
        self.store.update_record("bearlychen", "2855270041", state="transfer_started")
        self.store.update_record(
            "bearlychen", "2855270041", youtube_video_id="video_123", state="video_confirmed"
        )
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270041", youtube_video_id=None)
        with self.assertRaises(upload_state.YouTubeUploadStateValidationError):
            self.store.update_record("bearlychen", "2855270042", state="needs_attention")
        record = self.store.get("bearlychen", "2855270041")
        self.assertEqual(record["source_download_job_id"], "38")
        self.assertEqual(record["source_download_item_id"], "38-item-1")
        self.assertEqual(record["media_path"], "bearlychen/video.mp4")

    def test_update_failure_does_not_change_durable_record(self):
        original, _ = self._create()
        previous = self.path.read_bytes()
        with mock.patch.object(
            upload_state, "atomic_write_text", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaises(upload_state.YouTubeUploadStatePersistenceError):
                self.store.update_record(
                    "bearlychen", "2855270041", reason="api_unavailable"
                )
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.store.get("bearlychen", "2855270041"), original)

    def test_completed_and_many_records_survive_reload_without_retention(self):
        self._create()
        self.store.update_record("bearlychen", "2855270041", upload_job_id="45", state="upload_queued")
        self.store.update_record("bearlychen", "2855270041", state="transfer_started")
        self.store.update_record("bearlychen", "2855270041", youtube_video_id="video_123", state="video_confirmed")
        self.store.update_record("bearlychen", "2855270041", state="completed")
        for index in range(200):
            vod_id = str(2856000000 + index)
            self._create(twitch_vod_id=vod_id, source_download_job_id=str(1000 + index), source_download_item_id=f"{1000 + index}-item-1", media_path=f"bearlychen/{vod_id}.mp4", size_bytes=index)
        reloaded = upload_state.YouTubeUploadStateStore(self.path, clock=lambda: NOW).load()
        self.assertEqual(len(reloaded["uploads"]), 201)
        self.assertEqual(reloaded["uploads"]["bearlychen:2855270041"]["state"], "completed")
        self.assertFalse((self.dashboard_dir / "bearlychen" / "video.mp4").exists())

    def test_exact_schema_rejects_tokens_cookies_and_api_response_fields(self):
        record = self._record(token="secret")
        self.path.write_text(
            json.dumps({"version": 1, "uploads": {"bearlychen:2855270041": record}}),
            encoding="utf-8",
        )
        with self.assertRaises(upload_state.YouTubeUploadStateLoadError):
            self.store.load()
