from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_plan as plan_module
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard import youtube_upload_state


VOD_ID = "2855270041"


class AutoYouTubePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.store = youtube_upload_state.YouTubeUploadStateStore(
            self.root / "youtube-upload-state.json"
        )
        self.settings = {
            "youtube_title_template": "{streamer}: {title}",
            "youtube_description_template": "{title}\n{url}",
            "youtube_description": "fallback",
            "youtube_privacy_status": "unlisted",
            "youtube_category_id": "20",
            "youtube_tags": "twitch, archive",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_media(self, *, size=b"media") -> Path:
        path = self.media_root / "bearlychen" / "vod.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(size)
        path.with_suffix(".info.json").write_text(
            json.dumps({"id": VOD_ID, "webpage_url": f"https://www.twitch.tv/videos/{VOD_ID}"}),
            encoding="utf-8",
        )
        return path

    def _intent(self, *, inputs=None, playlist_id="PLAYLIST_A"):
        path = self._write_media()
        return self.store.create_intent_if_absent(
            "BearLyChen",
            VOD_ID,
            source_download_job_id="12",
            source_download_item_id="12-item-1",
            media_path="bearlychen/vod.mkv",
            size_bytes=path.stat().st_size,
            playlist_id=playlist_id,
            plan_inputs=(
                plan_module.freeze_plan_inputs(self.settings)
                if inputs is None
                else inputs
            ),
        )[0]

    def _service(self, builder):
        return plan_module.AutoYouTubePlanService(
            state_store=self.store,
            media_policy=MediaPathPolicy(self.media_root),
            metadata_builder=builder,
        )

    def test_plan_reuses_final_sanitizers_and_frozen_metadata_inputs(self):
        record = self._intent()
        builder = mock.Mock(return_value={
            "title": "A < B",
            "description": "Hallüüü <3\nNext line",
        })

        self.assertEqual(self._service(builder).prepare_record(record), "ready")
        saved = self.store.get("bearlychen", VOD_ID)
        upload_plan = saved["upload_plan"]

        builder.assert_called_once()
        builder_settings = builder.call_args.args[1]
        self.assertEqual(builder_settings["youtube_title_template"], self.settings["youtube_title_template"])
        self.assertEqual(builder_settings["youtube_description_template"], self.settings["youtube_description_template"])
        self.assertEqual(upload_plan, {
            "title": "A B",
            "description": "Hallüüü 3\nNext line",
            "privacy_status": "unlisted",
            "category_id": "20",
            "tags": ["twitch", "archive"],
        })
        self.assertEqual(saved["state"], "plan_ready")
        self.assertEqual(saved["playlist_id"], "PLAYLIST_A")
        self.assertEqual(saved["playlist_id"], "PLAYLIST_A")
        self.assertIsNone(saved["upload_job_id"])
        self.assertEqual(saved["parts"], [])

    def test_plan_is_immutable_across_reconciliation_settings_and_restart(self):
        record = self._intent()
        builder = mock.Mock(return_value={"title": "Original", "description": "First"})
        service = self._service(builder)
        self.assertEqual(service.prepare_record(record), "ready")
        original = self.store.get("bearlychen", VOD_ID)["upload_plan"]
        self.settings.update({
            "youtube_title_template": "NEW {title}",
            "youtube_privacy_status": "public",
            "youtube_tags": "changed",
        })
        builder.reset_mock()

        self.assertEqual(service.reconcile()["ready"], 1)
        builder.assert_not_called()
        restarted = youtube_upload_state.YouTubeUploadStateStore(self.store.path)
        self.assertEqual(restarted.get("bearlychen", VOD_ID)["upload_plan"], original)
        self.assertEqual(restarted.get("bearlychen", VOD_ID)["playlist_id"], "PLAYLIST_A")

    def test_missing_or_changed_source_becomes_attention_without_guessing(self):
        record = self._intent()
        (self.media_root / "bearlychen" / "vod.mkv").unlink()
        self.assertEqual(self._service(mock.Mock()).prepare_record(record), "attention")
        missing = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(missing["state"], "needs_attention")
        self.assertEqual(missing["reason"], "plan_media_missing")

        self.store = youtube_upload_state.YouTubeUploadStateStore(self.root / "changed.json")
        record = self._intent()
        (self.media_root / "bearlychen" / "vod.mkv").write_bytes(b"changed-size")
        self.assertEqual(self._service(mock.Mock()).prepare_record(record), "attention")
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["reason"], "plan_source_invalid")

    def test_wrong_vod_sidecar_or_metadata_failure_never_creates_a_plan(self):
        record = self._intent()
        sidecar = self.media_root / "bearlychen" / "vod.info.json"
        sidecar.write_text(json.dumps({"id": "2855270042"}), encoding="utf-8")
        self.assertEqual(self._service(mock.Mock()).prepare_record(record), "attention")
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["reason"], "plan_source_invalid")

        self.store = youtube_upload_state.YouTubeUploadStateStore(self.root / "metadata-error.json")
        record = self._intent()
        builder = mock.Mock(side_effect=RuntimeError("metadata unavailable"))
        self.assertEqual(self._service(builder).prepare_record(record), "attention")
        current = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(current["reason"], "plan_preparation_failed")
        self.assertIsNone(current["upload_plan"])

    def test_legacy_intent_without_frozen_inputs_is_not_reinterpreted(self):
        path = self._write_media()
        record, _ = self.store.create_intent_if_absent(
            "bearlychen", VOD_ID,
            source_download_job_id="12", source_download_item_id="12-item-1",
            media_path="bearlychen/vod.mkv", size_bytes=path.stat().st_size,
        )
        builder = mock.Mock()
        self.assertEqual(self._service(builder).prepare_record(record), "attention")
        builder.assert_not_called()
        current = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(current["reason"], "plan_inputs_missing")

    def test_plan_write_failure_leaves_no_partial_plan_and_recovery_is_idempotent(self):
        record = self._intent()
        builder = mock.Mock(return_value={"title": "Title", "description": "Description"})
        service = self._service(builder)
        with mock.patch.object(
            self.store, "set_upload_plan", side_effect=youtube_upload_state.YouTubeUploadStatePersistenceError("disk full")
        ):
            self.assertEqual(service.prepare_record(record), "pending")
        current = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(current["state"], "intent_pending")
        self.assertIsNone(current["upload_plan"])
        self.assertEqual(service.reconcile()["ready"], 1)
        self.assertEqual(self.store.get("bearlychen", VOD_ID)["state"], "plan_ready")

    def test_upload_plan_schema_rejects_absolute_paths_and_unsanitized_values(self):
        with self.assertRaises(youtube_upload_state.YouTubeUploadStateValidationError):
            self.store.create_intent_if_absent(
                "bearlychen", VOD_ID,
                source_download_job_id="12", source_download_item_id="12-item-1",
                media_path="C:\\absolute\\video.mkv", size_bytes=1,
            )
        record = self._intent()
        with self.assertRaises(youtube_upload_state.YouTubeUploadStateValidationError):
            self.store.set_upload_plan("bearlychen", VOD_ID, {
                "title": "Bad < Title", "description": "safe",
                "privacy_status": "private", "category_id": "20", "tags": [],
            })
        self.assertEqual(record["state"], "intent_pending")
