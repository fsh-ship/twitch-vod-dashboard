from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from vod_dashboard import auto_vod_result
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard import job_store
from vod_dashboard.jobs import (
    DOWNLOAD_FINAL_OUTPUT_MARKER,
    DownloadWorkerDependencies,
    JobManager,
    JobPersistenceRequiredError,
    parse_download_final_output_marker,
    run_download_job,
)


VOD_ID = "2855270041"


class AutoVodCompletedResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_video(self, relative: str = "bearlychen/video.mp4") -> Path:
        path = self.media_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"final media bytes")
        path.with_suffix(".info.json").write_text(
            json.dumps({"id": VOD_ID, "webpage_url": f"https://www.twitch.tv/videos/{VOD_ID}"}),
            encoding="utf-8",
        )
        return path

    def _resolve(self, raw_path, expected=VOD_ID):
        return auto_vod_result.resolve_completed_auto_vod_output(
            raw_path,
            {},
            expected,
            media_policy=MediaPathPolicy(self.media_root),
        )

    def _manager_and_job(self):
        manager = JobManager()
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: bearlychen",
            origin="auto_vod",
            streamer="BearLyChen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        return manager, job_id

    def _dependencies(self, lines, resolver):
        process = mock.Mock()
        process.stdout = list(lines)
        process.wait.return_value = 0
        return DownloadWorkerDependencies(
            load_settings=lambda: {
                "batch_postprocess_mode": "after_each",
                "twitch_rate_limit": "",
                "youtube_enabled": False,
                "youtube_auto_upload": False,
            },
            clean_postprocess_mode=lambda _value: "after_each",
            clean_rate_limit=lambda _value: "",
            append_log=lambda _job_id, _line: None,
            snapshot_video_files=lambda _settings: {},
            new_video_files=lambda _before, _after: [],
            recently_changed_video_files=lambda *_args, **_kwargs: [],
            prepare_manual_upload=mock.Mock(),
            get_youtube_service=mock.Mock(),
            upload_to_youtube=mock.Mock(),
            build_download_command=lambda _urls, _settings: (["yt-dlp"], self.root / "urls.txt"),
            download_directory=lambda _settings: self.media_root,
            popen=mock.Mock(return_value=process),
            clock=lambda: 1.0,
            resolve_auto_vod_completed_output=resolver,
        )

    def test_final_output_marker_parser_ignores_unrelated_lines(self):
        self.assertIsNone(parse_download_final_output_marker("[download] 10%"))
        self.assertIsNone(parse_download_final_output_marker(DOWNLOAD_FINAL_OUTPUT_MARKER))
        self.assertEqual(
            parse_download_final_output_marker(
                f"notice {DOWNLOAD_FINAL_OUTPUT_MARKER}/output/merged.mkv"
            ),
            "/output/merged.mkv",
        )

    def test_exact_final_auto_vod_result_uses_sidecar_identity_and_relative_path(self):
        video = self._write_video()
        result = self._resolve(video)
        self.assertEqual(result["completed_media_path"], "bearlychen/video.mp4")
        self.assertEqual(result["completed_media_size_bytes"], len(b"final media bytes"))
        self.assertEqual(result["completed_twitch_vod_id"], VOD_ID)
        self.assertNotIn(str(self.media_root), json.dumps(result))

    def test_result_validation_rejects_outside_missing_non_media_and_mismatch(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        invalid_part = self.media_root / "bearlychen/video.mp4.part"
        invalid_part.parent.mkdir(exist_ok=True)
        invalid_part.write_bytes(b"partial")
        sidecar = self.media_root / "bearlychen/video.info.json"
        sidecar.write_text("{}", encoding="utf-8")
        video = self._write_video("bearlychen/mismatch.mp4")
        video.with_suffix(".info.json").write_text(
            json.dumps({"id": "2855270042"}), encoding="utf-8"
        )
        non_regular = self.media_root / "bearlychen" / "directory.mp4"
        non_regular.mkdir()
        for path in (
            outside,
            invalid_part,
            sidecar,
            video,
            non_regular,
            self.media_root / "missing.mp4",
        ):
            with self.subTest(path=path.name):
                with self.assertRaises(RuntimeError):
                    self._resolve(path)

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        link = self.media_root / "bearlychen" / "link.mp4"
        link.parent.mkdir(exist_ok=True)
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("Symlinks are unavailable in this test environment")
        with self.assertRaises(RuntimeError):
            self._resolve(link)

    def test_auto_worker_persists_only_the_marked_final_output_before_completion(self):
        manager, job_id = self._manager_and_job()
        resolved = {
            "completed_media_path": "bearlychen/merged.mkv",
            "completed_media_size_bytes": 987,
            "completed_twitch_vod_id": VOD_ID,
        }
        resolver = mock.Mock(return_value=resolved)
        dependencies = self._dependencies(
            [
                "ordinary yt-dlp output\n",
                f"{DOWNLOAD_FINAL_OUTPUT_MARKER}/absolute/old.part\n",
                f"{DOWNLOAD_FINAL_OUTPUT_MARKER}/absolute/merged.mkv\n",
            ],
            resolver,
        )

        run_download_job(job_id, manager, dependencies)

        resolver.assert_called_once_with(
            "/absolute/merged.mkv", mock.ANY, VOD_ID
        )
        job = manager.get_job(job_id)
        self.assertEqual(job["item_states"], ["completed"])
        self.assertEqual(job["completed_media_path"], "bearlychen/merged.mkv")
        self.assertEqual(job["completed_media_size_bytes"], 987)
        self.assertEqual(job["completed_twitch_vod_id"], VOD_ID)
        self.assertNotIn("/absolute/merged.mkv", "\n".join(job["log"]))
        self.assertFalse((self.root / "youtube-upload-state.json").exists())
        dependencies.get_youtube_service.assert_not_called()
        dependencies.upload_to_youtube.assert_not_called()

    def test_missing_or_invalid_marker_never_marks_auto_vod_completed(self):
        for lines, resolver in (
            (["ordinary output\n"], mock.Mock()),
            (
                [f"{DOWNLOAD_FINAL_OUTPUT_MARKER}/absolute/video.mp4.part\n"],
                mock.Mock(side_effect=RuntimeError("not a final media file")),
            ),
        ):
            with self.subTest(lines=lines):
                manager, job_id = self._manager_and_job()
                run_download_job(job_id, manager, self._dependencies(lines, resolver))
                job = manager.get_job(job_id)
                self.assertEqual(job["item_states"], ["failed"])
                self.assertNotIn("completed_media_path", job)
                self.assertNotIn("completed_media_size_bytes", job)
                self.assertNotIn("completed_twitch_vod_id", job)

    def test_manual_download_remains_compatible_without_final_marker(self):
        manager = JobManager()
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"], "Manual"
        )
        run_download_job(
            job_id,
            manager,
            self._dependencies(["ordinary output\n"], mock.Mock()),
        )
        job = manager.get_job(job_id)
        self.assertEqual(job["item_states"], ["completed"])
        self.assertNotIn("completed_media_path", job)

    def test_job_store_persists_result_and_rejects_unsafe_optional_fields(self):
        manager, job_id = self._manager_and_job()
        claim = manager.claim_next_item(job_id)
        manager.finish_auto_vod_download_with_result(
            job_id,
            claim["item_id"],
            completed_media_path="bearlychen/video.mp4",
            completed_media_size_bytes=12,
            completed_twitch_vod_id=VOD_ID,
        )
        job = manager.get_job(job_id)
        serialized = job_store.serialize_job(job)
        self.assertEqual(serialized["completed_media_path"], "bearlychen/video.mp4")
        for field, value in (
            ("completed_media_path", "../video.mp4"),
            ("completed_media_path", "bearlychen/video.info.json"),
            ("completed_media_path", "bearlychen/video.mp4.part"),
            ("completed_media_size_bytes", True),
            ("completed_twitch_vod_id", f"v{VOD_ID}"),
            ("completed_twitch_vod_id", "2855270042"),
        ):
            with self.subTest(field=field, value=value):
                unsafe = dict(job)
                unsafe[field] = value
                with self.assertRaises(job_store.JobStoreValidationError):
                    job_store.serialize_job(unsafe)
        historical = dict(job)
        for key in (
            "completed_media_path",
            "completed_media_size_bytes",
            "completed_twitch_vod_id",
        ):
            historical.pop(key)
        self.assertNotIn("completed_media_path", job_store.serialize_job(historical))

    def test_required_result_persistence_failure_leaves_no_ready_completion(self):
        store = job_store.JobStore(self.root / "jobs.json")
        manager = JobManager(job_store=store, media_root=self.media_root)
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: bearlychen",
            origin="auto_vod",
            streamer="bearlychen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        claim = manager.claim_next_item(job_id)
        with mock.patch.object(store, "save", side_effect=OSError("ENOSPC")):
            with self.assertRaises(JobPersistenceRequiredError):
                manager.finish_auto_vod_download_with_result(
                    job_id,
                    claim["item_id"],
                    completed_media_path="bearlychen/video.mp4",
                    completed_media_size_bytes=12,
                    completed_twitch_vod_id=VOD_ID,
                )
        runtime = manager.get_job(job_id)
        self.assertEqual(runtime["item_states"], ["failed"])
        self.assertNotIn("completed_media_path", runtime)
        restored = store.load().state["jobs"][0]
        self.assertNotIn("completed_media_path", restored)

    def test_restart_restores_exact_result_with_existing_source_identity(self):
        store = job_store.JobStore(self.root / "jobs.json")
        manager = JobManager(job_store=store, media_root=self.media_root)
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: BearLyChen",
            origin="auto_vod",
            streamer="BearLyChen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        claim = manager.claim_next_item(job_id)
        manager.finish_auto_vod_download_with_result(
            job_id,
            claim["item_id"],
            completed_media_path="bearlychen/final.mkv",
            completed_media_size_bytes=321,
            completed_twitch_vod_id=VOD_ID,
        )

        restarted = JobManager(job_store=store, media_root=self.media_root)
        restarted.restore_from_store()
        result = restarted.get_job(job_id)

        self.assertEqual(result["id"], job_id)
        self.assertEqual(result["item_ids"], [claim["item_id"]])
        self.assertEqual(result["streamer"], "bearlychen")
        self.assertEqual(result["twitch_vod_id"], VOD_ID)
        self.assertEqual(result["completed_media_path"], "bearlychen/final.mkv")
        self.assertEqual(result["completed_media_size_bytes"], 321)
        self.assertEqual(result["completed_twitch_vod_id"], VOD_ID)

    def test_historical_auto_vod_without_completed_result_still_reloads(self):
        store = job_store.JobStore(self.root / "jobs.json")
        manager = JobManager(job_store=store, media_root=self.media_root)
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Historical Auto VOD",
            origin="auto_vod",
            streamer="bearlychen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        claim = manager.claim_next_item(job_id)
        manager.finish_claimed_item(job_id, claim["item_id"], "completed")

        restarted = JobManager(job_store=store, media_root=self.media_root)
        restarted.restore_from_store()
        historical = restarted.get_job(job_id)
        self.assertEqual(historical["item_states"], ["completed"])
        self.assertNotIn("completed_media_path", historical)

    def test_completed_result_is_not_observable_until_required_save_finishes(self):
        store = job_store.JobStore(self.root / "jobs.json")
        manager = JobManager(job_store=store, media_root=self.media_root)
        job_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Automatic Twitch VOD: bearlychen",
            origin="auto_vod",
            streamer="bearlychen",
            twitch_vod_id=VOD_ID,
            attempt=1,
            post_download_mode="download_only",
        )
        claim = manager.claim_next_item(job_id)
        original_save = store.save
        entered = threading.Event()
        release = threading.Event()
        observed = threading.Event()

        def delayed_save(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return original_save(*args, **kwargs)

        def complete():
            manager.finish_auto_vod_download_with_result(
                job_id,
                claim["item_id"],
                completed_media_path="bearlychen/video.mp4",
                completed_media_size_bytes=12,
                completed_twitch_vod_id=VOD_ID,
            )

        def observe():
            job = manager.get_job(job_id)
            self.assertEqual(job["item_states"], ["completed"])
            self.assertEqual(job["completed_media_path"], "bearlychen/video.mp4")
            observed.set()

        with mock.patch.object(store, "save", side_effect=delayed_save):
            finisher = threading.Thread(target=complete)
            finisher.start()
            self.assertTrue(entered.wait(timeout=2))
            reader = threading.Thread(target=observe)
            reader.start()
            self.assertFalse(observed.wait(timeout=0.1))
            release.set()
            finisher.join(timeout=2)
            reader.join(timeout=2)
        self.assertTrue(observed.is_set())

    def test_retry_child_establishes_its_own_result_without_parent_donation(self):
        manager, parent_id = self._manager_and_job()
        parent_claim = manager.claim_next_item(parent_id)
        manager.finish_claimed_item(parent_id, parent_claim["item_id"], "failed")
        child_id = manager.create_download_job(
            [f"https://www.twitch.tv/videos/{VOD_ID}"],
            "Retry",
            retry_of={"job_id": parent_id, "item_id": parent_claim["item_id"]},
            origin="auto_vod",
            streamer="bearlychen",
            twitch_vod_id=VOD_ID,
            attempt=2,
            post_download_mode="download_only",
        )
        child_claim = manager.claim_next_item(child_id)
        manager.finish_auto_vod_download_with_result(
            child_id,
            child_claim["item_id"],
            completed_media_path="bearlychen/retry.mp4",
            completed_media_size_bytes=22,
            completed_twitch_vod_id=VOD_ID,
        )
        self.assertNotIn("completed_media_path", manager.get_job(parent_id))
        self.assertEqual(
            manager.get_job(child_id)["completed_media_path"],
            "bearlychen/retry.mp4",
        )
