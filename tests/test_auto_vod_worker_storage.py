import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard.auto_vod_storage import AutoVodStorageStatus
from vod_dashboard.job_store import JobStore, JobStoreValidationError
from vod_dashboard.jobs import (
    DownloadWorkerDependencies,
    JobManager,
    JobPersistenceRequiredError,
    run_download_job,
)


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class DeferredThread(ImmediateThread):
    def start(self):
        self.started = True


class AutoVodWorkerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = JobManager(now=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def create_auto_job(self, manager=None):
        target = manager or self.manager
        return target.create_download_job(
            ["https://www.twitch.tv/videos/2854443252"],
            "Automatic Twitch VOD: alpha",
            origin="auto_vod",
            streamer="alpha",
            twitch_vod_id="2854443252",
            attempt=1,
            post_download_mode="download_only",
        )

    def dependencies(self, status, *, popen=None, assessor=None):
        process = mock.Mock()
        process.stdout = [
            "VOD-DASHBOARD-FINAL-FILE=/temporary/alpha/video.mp4\n"
        ]
        process.wait.return_value = 0
        popen = popen or mock.Mock(return_value=process)
        list_path = self.root / "download-list.txt"
        return DownloadWorkerDependencies(
            load_settings=lambda: {
                "batch_postprocess_mode": "after_each",
                "twitch_rate_limit": "",
                "youtube_enabled": False,
                "youtube_auto_upload": False,
                "youtube_privacy_status": "private",
            },
            clean_postprocess_mode=lambda value: "after_each",
            clean_rate_limit=lambda value: "",
            append_log=self.manager.append_job_log,
            snapshot_video_files=lambda settings: {},
            new_video_files=lambda before, after: [],
            recently_changed_video_files=lambda *args, **kwargs: [],
            prepare_manual_upload=lambda path, settings, **kwargs: path,
            get_youtube_service=mock.Mock(),
            upload_to_youtube=mock.Mock(),
            build_download_command=lambda urls, settings: (["yt-dlp", *urls], list_path),
            download_directory=lambda settings: self.root,
            popen=popen,
            clock=lambda: 1.0,
            storage_assessor=assessor or (lambda path: status),
            resolve_auto_vod_completed_output=lambda *_args: {
                "completed_media_path": "alpha/video.mp4",
                "completed_media_size_bytes": 123,
                "completed_twitch_vod_id": "2854443252",
            },
        ), popen

    def test_low_and_unavailable_storage_block_before_popen(self):
        for state, reason in (
            ("insufficient", "insufficient_storage"),
            ("unavailable", "storage_unavailable"),
        ):
            with self.subTest(state=state):
                self.manager = JobManager(now=lambda: NOW)
                job_id = self.create_auto_job()
                status = AutoVodStorageStatus(
                    state,
                    1 if state == "insufficient" else None,
                    200 if state == "insufficient" else None,
                    50 if state == "insufficient" else None,
                )
                dependencies, popen = self.dependencies(status)

                run_download_job(job_id, self.manager, dependencies)

                popen.assert_not_called()
                job = self.manager.get_job(job_id)
                self.assertEqual(job["state"], "queued")
                self.assertEqual(job["item_states"], ["queued"])
                self.assertTrue(job["storage_blocked"])
                self.assertEqual(job["blocking_reason"], reason)
                self.assertEqual(job["attempt"], 1)
                self.assertEqual(len(self.manager.jobs), 1)
                self.assertFalse(
                    self.manager.queue_controls_snapshot()["download"]["has_active_item"]
                )

    def test_exact_threshold_allows_auto_vod_popen(self):
        job_id = self.create_auto_job()
        status = AutoVodStorageStatus("sufficient", 50, 200, 50)
        dependencies, popen = self.dependencies(status)

        run_download_job(job_id, self.manager, dependencies)

        popen.assert_called_once()
        self.assertEqual(self.manager.get_job(job_id)["state"], "completed")

    def test_storage_measurement_exception_fails_closed_as_unavailable(self):
        job_id = self.create_auto_job()
        dependencies, popen = self.dependencies(
            AutoVodStorageStatus("sufficient", 100, 200, 50),
            assessor=mock.Mock(side_effect=OSError("secret device")),
        )

        run_download_job(job_id, self.manager, dependencies)

        popen.assert_not_called()
        job = self.manager.get_job(job_id)
        self.assertTrue(job["storage_blocked"])
        self.assertEqual(job["blocking_reason"], "storage_unavailable")

    def test_manual_download_bypasses_storage_assessor(self):
        job_id = self.manager.create_download_job(
            ["https://www.twitch.tv/videos/2854443252"], "Manual"
        )
        assessor = mock.Mock(side_effect=OSError("secret mount"))
        dependencies, popen = self.dependencies(
            AutoVodStorageStatus("unavailable", None, None, None),
            assessor=assessor,
        )

        run_download_job(job_id, self.manager, dependencies)

        assessor.assert_not_called()
        popen.assert_called_once()

    def test_storage_block_persists_before_clean_worker_exit(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: NOW)
        manager = JobManager(
            now=lambda: NOW, job_store=store, media_root=self.root
        )
        self.manager = manager
        job_id = self.create_auto_job(manager)
        dependencies, popen = self.dependencies(
            AutoVodStorageStatus("insufficient", 1, 200, 50)
        )

        run_download_job(job_id, manager, dependencies)

        popen.assert_not_called()
        durable = store.load().jobs[0]
        self.assertEqual(durable["id"], job_id)
        self.assertEqual(durable["item_states"], ["queued"])
        self.assertTrue(durable["storage_blocked"])
        self.assertEqual(durable["blocking_reason"], "insufficient_storage")

    def test_block_persistence_failure_prevents_popen_without_retry(self):
        job_id = self.create_auto_job()
        dependencies, popen = self.dependencies(
            AutoVodStorageStatus("insufficient", 1, 200, 50)
        )
        original = self.manager._persist_required
        calls = {"count": 0}

        def fail_second(snapshot):
            calls["count"] += 1
            if calls["count"] == 2:
                raise JobPersistenceRequiredError()
            return original(snapshot)

        with mock.patch.object(self.manager, "_persist_required", side_effect=fail_second):
            run_download_job(job_id, self.manager, dependencies)

        popen.assert_not_called()
        job = self.manager.get_job(job_id)
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["storage_blocked"])
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_blocked_auto_vod_does_not_starve_manual_job(self):
        auto_id = self.create_auto_job()
        manual_id = self.manager.create_download_job(
            ["https://www.twitch.tv/videos/2854443251"], "Manual"
        )
        low_dependencies, auto_popen = self.dependencies(
            AutoVodStorageStatus("insufficient", 1, 200, 50)
        )
        run_download_job(auto_id, self.manager, low_dependencies)

        manual_dependencies, manual_popen = self.dependencies(
            AutoVodStorageStatus("unavailable", None, None, None)
        )
        run_download_job(manual_id, self.manager, manual_dependencies)

        auto_popen.assert_not_called()
        manual_popen.assert_called_once()
        self.assertTrue(self.manager.get_job(auto_id)["storage_blocked"])
        self.assertEqual(self.manager.get_job(manual_id)["state"], "completed")

    def test_shutdown_before_authorization_prevents_popen(self):
        job_id = self.create_auto_job()

        def stop_then_allow(path):
            self.manager.begin_shutdown()
            return AutoVodStorageStatus("sufficient", 100, 200, 50)

        dependencies, popen = self.dependencies(
            AutoVodStorageStatus("sufficient", 100, 200, 50),
            assessor=stop_then_allow,
        )
        run_download_job(job_id, self.manager, dependencies)
        popen.assert_not_called()

    def test_storage_recovery_rearms_same_job_once_without_new_attempt(self):
        job_id = self.create_auto_job()
        low, first_popen = self.dependencies(
            AutoVodStorageStatus("insufficient", 1, 200, 50)
        )
        run_download_job(job_id, self.manager, low)
        sufficient, resumed_popen = self.dependencies(
            AutoVodStorageStatus("sufficient", 100, 200, 50)
        )

        target = lambda value: run_download_job(value, self.manager, sufficient)
        first = self.manager.rearm_storage_blocked_download(
            job_id, target, thread_factory=ImmediateThread
        )
        second = self.manager.rearm_storage_blocked_download(
            job_id, target, thread_factory=ImmediateThread
        )

        self.assertTrue(first)
        self.assertFalse(second)
        first_popen.assert_not_called()
        resumed_popen.assert_called_once()
        self.assertEqual(list(self.manager.jobs), [job_id])
        job = self.manager.get_job(job_id)
        self.assertEqual(job["attempt"], 1)
        self.assertFalse(job["storage_blocked"])
        self.assertEqual(job["state"], "completed")

    def test_rearm_authorization_persistence_failure_never_starts_popen(self):
        job_id = self.create_auto_job()
        claimed = self.manager.claim_next_item(job_id)
        self.manager.block_auto_vod_for_storage(
            job_id, claimed["item_id"], "insufficient_storage"
        )
        sufficient, popen = self.dependencies(
            AutoVodStorageStatus("sufficient", 100, 200, 50)
        )
        calls = {"count": 0}

        def fail_authorization(snapshot):
            calls["count"] += 1
            if calls["count"] == 2:
                raise JobPersistenceRequiredError()

        with mock.patch.object(
            self.manager, "_persist_required", side_effect=fail_authorization
        ):
            self.manager.rearm_storage_blocked_download(
                job_id,
                lambda value: run_download_job(value, self.manager, sufficient),
                thread_factory=ImmediateThread,
            )

        popen.assert_not_called()
        job = self.manager.get_job(job_id)
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["storage_blocked"])
        self.assertEqual(job["attempt"], 1)

    def test_repeated_rearm_does_not_create_duplicate_worker(self):
        job_id = self.create_auto_job()
        claimed = self.manager.claim_next_item(job_id)
        self.manager.block_auto_vod_for_storage(
            job_id, claimed["item_id"], "insufficient_storage"
        )
        factory = mock.Mock(side_effect=DeferredThread)

        first = self.manager.rearm_storage_blocked_download(
            job_id, mock.Mock(), thread_factory=factory
        )
        second = self.manager.rearm_storage_blocked_download(
            job_id, mock.Mock(), thread_factory=factory
        )

        self.assertTrue(first)
        self.assertFalse(second)
        factory.assert_called_once()

    def test_rearmed_claim_persistence_failure_restores_same_blocked_job(self):
        job_id = self.create_auto_job()
        claimed = self.manager.claim_next_item(job_id)
        self.manager.block_auto_vod_for_storage(
            job_id, claimed["item_id"], "insufficient_storage"
        )
        self.manager._storage_rearmed_jobs.add(job_id)

        with mock.patch.object(
            self.manager,
            "_persist_required",
            side_effect=JobPersistenceRequiredError(),
        ):
            with self.assertRaises(JobPersistenceRequiredError):
                self.manager.claim_next_item(job_id)

        job = self.manager.get_job(job_id)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["item_states"], ["queued"])
        self.assertTrue(job["storage_blocked"])
        self.assertEqual(job["attempt"], 1)

        self.assertFalse(
            self.manager.queue_controls_snapshot()["download"]["has_active_item"]
        )

    def test_blocked_job_survives_restore_and_remains_rearmable(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: NOW)
        manager = JobManager(now=lambda: NOW, job_store=store, media_root=self.root)
        self.manager = manager
        job_id = self.create_auto_job(manager)
        dependencies, _ = self.dependencies(
            AutoVodStorageStatus("insufficient", 1, 200, 50)
        )
        run_download_job(job_id, manager, dependencies)

        restored = JobManager(now=lambda: NOW, job_store=store, media_root=self.root)
        result = restored.restore_from_store()
        job = restored.get_job(job_id)

        self.assertEqual(result.reconciled_item_count, 0)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["item_states"], ["queued"])
        self.assertTrue(job["storage_blocked"])
        self.assertEqual(job["attempt"], 1)

        self.manager = restored
        sufficient, popen = self.dependencies(
            AutoVodStorageStatus("sufficient", 100, 200, 50)
        )
        rearmed = restored.rearm_storage_blocked_download(
            job_id,
            lambda value: run_download_job(value, restored, sufficient),
            thread_factory=ImmediateThread,
        )

        self.assertTrue(rearmed)
        popen.assert_called_once()
        durable = store.load().jobs
        self.assertEqual([item["id"] for item in durable], [job_id])
        self.assertEqual(durable[0]["attempt"], 1)
        self.assertFalse(durable[0]["storage_blocked"])

    def test_job_store_defaults_old_jobs_and_rejects_unsafe_block_fields(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: NOW)
        manager = JobManager(now=lambda: NOW)
        job_id = self.create_auto_job(manager)
        legacy = manager.get_job(job_id)
        legacy.pop("storage_blocked")
        legacy.pop("blocking_reason")
        store.save([legacy], next_job_id=2, revision=1, media_root=self.root)
        loaded = store.load().jobs[0]
        self.assertFalse(loaded["storage_blocked"])
        self.assertEqual(loaded["blocking_reason"], "")

        invalid = dict(legacy)
        invalid["storage_blocked"] = True
        invalid["blocking_reason"] = "secret_path"
        with self.assertRaises(JobStoreValidationError):
            store.save([invalid], next_job_id=2, revision=2, media_root=self.root)


if __name__ == "__main__":
    unittest.main()
