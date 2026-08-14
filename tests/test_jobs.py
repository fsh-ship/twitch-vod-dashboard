import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from vod_dashboard.jobs import (
    DownloadWorkerDependencies,
    JobManager,
    UploadWorkerDependencies,
    run_download_job,
    run_upload_job,
)


class JobManagerTests(unittest.TestCase):
    def manager(self):
        return JobManager(now=lambda: datetime(2026, 8, 11, 12, 34, 56))

    def test_first_and_monotonically_increasing_job_ids(self):
        manager = self.manager()

        self.assertEqual(manager.create_download_job([], "First"), "1")
        self.assertEqual(manager.create_download_job([], "Second"), "2")
        self.assertEqual(manager.create_upload_job([], "Third"), "3")

    def test_concurrent_job_creation_keeps_ids_unique_and_monotonic(self):
        manager = self.manager()
        created_ids = []
        result_lock = threading.Lock()

        def create_one(index):
            job_id = manager.create_download_job([], f"Job {index}")
            with result_lock:
                created_ids.append(job_id)

        threads = [threading.Thread(target=create_one, args=(index,)) for index in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(map(int, created_ids)), list(range(1, 41)))
        self.assertEqual(list(manager.jobs), [str(index) for index in range(1, 41)])

    def test_download_job_schema_and_initial_state(self):
        manager = self.manager()
        urls = ["https://www.twitch.tv/videos/1234567890"]

        job_id = manager.create_download_job(urls, "One VOD")

        self.assertEqual(
            manager.jobs[job_id],
            {
                "id": "1",
                "label": "One VOD",
                "status": "wartet",
                "created": "2026-08-11 12:34:56",
                "urls": urls,
                "total_urls": 1,
                "item_statuses": ["wartet"],
                "log": [],
                "returncode": None,
            },
        )

    def test_upload_job_schema_is_preserved(self):
        manager = self.manager()

        job_id = manager.create_upload_job(["C:/media/vod.mp4"], "Upload")

        self.assertEqual(
            manager.jobs[job_id],
            {
                "id": "1",
                "label": "Upload",
                "status": "wartet",
                "created": "2026-08-11 12:34:56",
                "urls": ["C:/media/vod.mp4"],
                "log": [],
                "returncode": None,
                "type": "youtube_upload",
            },
        )

    def test_existing_internal_status_values_can_be_applied(self):
        manager = self.manager()
        job_id = manager.create_download_job([], "Status")

        for status in ("wartet", "läuft", "fertig", "fehler"):
            self.assertTrue(manager.update_job(job_id, status=status))
            self.assertEqual(manager.jobs[job_id]["status"], status)

    def test_log_append_format_callback_and_missing_job_behavior(self):
        manager = self.manager()
        job_id = manager.create_download_job([], "Log")
        messages = []

        self.assertTrue(
            manager.append_job_log(job_id, "entry with newline\r\n", messages.append)
        )
        self.assertEqual(manager.jobs[job_id]["log"], ["entry with newline"])
        self.assertEqual(messages, ["Job 1: entry with newline"])
        self.assertFalse(manager.append_job_log("missing", "ignored", messages.append))
        self.assertEqual(messages, ["Job 1: entry with newline"])

    def test_log_cap_keeps_exactly_the_newest_500_entries(self):
        manager = self.manager()
        job_id = manager.create_download_job([], "Bounded")

        for index in range(505):
            manager.append_job_log(job_id, f"line-{index}")

        self.assertEqual(len(manager.jobs[job_id]["log"]), 500)
        self.assertEqual(manager.jobs[job_id]["log"][0], "line-5")
        self.assertEqual(manager.jobs[job_id]["log"][-1], "line-504")

    def test_concurrent_log_append_is_thread_safe_and_bounded(self):
        manager = self.manager()
        job_id = manager.create_download_job([], "Concurrent")

        def append_batch(worker):
            for index in range(200):
                manager.append_job_log(job_id, f"{worker}-{index}")

        threads = [
            threading.Thread(target=append_batch, args=(worker,))
            for worker in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        entries = manager.jobs[job_id]["log"]
        self.assertEqual(len(entries), 500)
        self.assertEqual(len(set(entries)), 500)

    def test_jobs_are_independent(self):
        manager = self.manager()
        first = manager.create_download_job([], "First")
        second = manager.create_download_job([], "Second")

        manager.append_job_log(first, "first-only")
        manager.update_job(second, status="fehler", returncode=1)

        self.assertEqual(manager.jobs[first]["log"], ["first-only"])
        self.assertEqual(manager.jobs[first]["status"], "wartet")
        self.assertEqual(manager.jobs[second]["log"], [])
        self.assertEqual(manager.jobs[second]["status"], "fehler")

    def test_snapshot_order_and_detachment(self):
        manager = self.manager()
        manager.create_download_job([], "First")
        manager.create_download_job([], "Second")

        forward = manager.snapshot_jobs()
        reverse = manager.snapshot_jobs(reverse=True)
        forward[0]["log"].append("snapshot-only")

        self.assertEqual([job["id"] for job in forward], ["1", "2"])
        self.assertEqual([job["id"] for job in reverse], ["2", "1"])
        self.assertEqual(manager.jobs["1"]["log"], [])

    def test_lookup_and_update_of_missing_job_are_safe(self):
        manager = self.manager()

        self.assertIsNone(manager.get_job("missing"))
        self.assertFalse(manager.update_job("missing", status="fehler"))

    def test_unexpected_exit_fails_only_unfinished_download_items(self):
        manager = self.manager()
        job_id = manager.create_download_job(["one", "two", "three"], "Batch")
        manager.set_download_item_status(job_id, 1, "fertig")
        manager.set_download_item_status(job_id, 2, "läuft")

        manager.fail_unfinished_download_items(job_id)

        self.assertEqual(
            manager.jobs[job_id]["item_statuses"],
            ["fertig", "fehler", "fehler"],
        )

    def test_worker_thread_is_daemon_and_receives_job_id(self):
        manager = self.manager()
        target = mock.Mock()
        thread = mock.Mock()
        thread_factory = mock.Mock(return_value=thread)

        result = manager.start_worker(target, "job-7", thread_factory)

        self.assertIs(result, thread)
        thread_factory.assert_called_once_with(
            target=target, args=("job-7",), daemon=True
        )
        thread.start.assert_called_once_with()


class DownloadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = JobManager()
        self.settings = {
            "batch_postprocess_mode": "after_each",
            "twitch_rate_limit": "",
            "youtube_enabled": False,
            "youtube_auto_upload": False,
            "youtube_privacy_status": "private",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_job(self, urls=None):
        return self.manager.create_download_job(
            urls or ["https://www.twitch.tv/videos/1234567890"], "Download"
        )

    def dependencies(
        self,
        *,
        returncodes=(0,),
        candidates=(),
        prepare=None,
        service=None,
        upload=None,
        mode=None,
    ):
        process_calls = []
        returncodes = iter(returncodes)
        list_paths = []

        def build_command(urls, settings):
            list_path = self.root / f"batch-{len(list_paths)}.txt"
            list_path.write_text("temporary", encoding="utf-8")
            list_paths.append(list_path)
            return ["python", "-m", "yt_dlp", *urls], list_path

        def popen(command, **kwargs):
            self.assertEqual(self.manager.jobs["1"]["status"], "läuft")
            active_index = len(process_calls)
            self.assertEqual(
                self.manager.jobs["1"]["item_statuses"][active_index], "läuft"
            )
            process = mock.Mock()
            process.stdout = ["yt-dlp output\n"]
            process.wait.return_value = next(returncodes)
            process_calls.append((command, kwargs, process))
            return process

        snapshots = iter(({"old.mp4": 1.0}, {"old.mp4": 1.0, "new.mp4": 2.0}) * 10)
        candidate_paths = list(candidates)
        dependencies = DownloadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            clean_postprocess_mode=lambda value: mode or value or "after_each",
            clean_rate_limit=lambda value: str(value or ""),
            append_log=self.manager.append_job_log,
            snapshot_video_files=lambda settings: next(snapshots),
            new_video_files=mock.Mock(
                side_effect=lambda before, after: list(candidate_paths)
            ),
            recently_changed_video_files=mock.Mock(return_value=[]),
            prepare_manual_upload=prepare or mock.Mock(side_effect=lambda path, *_args, **_kwargs: path),
            get_youtube_service=service or mock.Mock(),
            upload_to_youtube=upload or mock.Mock(return_value="video-id"),
            build_download_command=build_command,
            download_directory=lambda settings: self.root,
            popen=popen,
            clock=lambda: 123.0,
        )
        return dependencies, process_calls, list_paths

    def test_download_success_transitions_and_postprocesses_new_file(self):
        job_id = self.create_job()
        video = self.root / "new.mp4"
        prepare = mock.Mock(return_value=video)
        dependencies, process_calls, list_paths = self.dependencies(
            candidates=[video], prepare=prepare
        )

        run_download_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual((job["status"], job["returncode"]), ("fertig", 0))
        self.assertEqual(job["item_statuses"], ["fertig"])
        prepare.assert_called_once_with(video, self.settings, job_id=job_id)
        dependencies.new_video_files.assert_called_once_with(
            {"old.mp4": 1.0}, {"old.mp4": 1.0, "new.mp4": 2.0}
        )
        self.assertEqual(process_calls[0][0][:3], ["python", "-m", "yt_dlp"])
        self.assertEqual(process_calls[0][1]["cwd"], str(self.root))
        self.assertFalse(list_paths[0].exists())
        self.assertIn("yt-dlp output", job["log"])

    def test_nonzero_subprocess_marks_job_failed_and_skips_postprocessing(self):
        job_id = self.create_job()
        prepare = mock.Mock()
        dependencies, _, _ = self.dependencies(returncodes=[9], prepare=prepare)

        run_download_job(job_id, self.manager, dependencies)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 1)
        self.assertEqual(self.manager.jobs[job_id]["item_statuses"], ["fehler"])
        prepare.assert_not_called()
        self.assertIn(
            "VOD 1/1 ended with error code 9. Continuing with the next VOD.",
            self.manager.jobs[job_id]["log"],
        )

    def test_no_new_file_uses_recent_fallback_and_logs_normal_empty_state(self):
        job_id = self.create_job()
        prepare = mock.Mock()
        dependencies, _, _ = self.dependencies(prepare=prepare)

        run_download_job(job_id, self.manager, dependencies)

        dependencies.recently_changed_video_files.assert_called_once_with(
            self.settings, 123.0, minutes_buffer=180
        )
        prepare.assert_not_called()
        self.assertIn(
            "Prepare for YouTube: no new completed VOD file found to rename or describe.",
            self.manager.jobs[job_id]["log"],
        )

    def test_postprocessing_failure_preserves_outer_worker_failure_behavior(self):
        job_id = self.create_job()
        video = self.root / "new.mp4"
        dependencies, _, _ = self.dependencies(
            candidates=[video],
            prepare=mock.Mock(side_effect=RuntimeError("prepare failed")),
        )

        run_download_job(job_id, self.manager, dependencies)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], -2)
        self.assertEqual(self.manager.jobs[job_id]["item_statuses"], ["fertig"])
        self.assertIn("Error: prepare failed", self.manager.jobs[job_id]["log"])

    def test_auto_upload_disabled_still_prepares_but_never_uploads(self):
        self.settings["youtube_enabled"] = True
        self.settings["youtube_auto_upload"] = False
        job_id = self.create_job()
        video = self.root / "new.mp4"
        prepare = mock.Mock(return_value=video)
        service = mock.Mock()
        upload = mock.Mock()
        dependencies, _, _ = self.dependencies(
            candidates=[video], prepare=prepare, service=service, upload=upload
        )

        run_download_job(job_id, self.manager, dependencies)

        prepare.assert_called_once()
        service.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")

    def test_auto_upload_enabled_prepares_all_but_uploads_only_first_candidate(self):
        self.settings["youtube_enabled"] = True
        self.settings["youtube_auto_upload"] = True
        job_id = self.create_job()
        videos = [self.root / "newest.mp4", self.root / "older.mp4"]
        prepare = mock.Mock(side_effect=lambda path, *_args, **_kwargs: path)
        service = mock.Mock()
        upload = mock.Mock(return_value="video-id")
        dependencies, _, _ = self.dependencies(
            candidates=videos, prepare=prepare, service=service, upload=upload
        )

        run_download_job(job_id, self.manager, dependencies)

        service.assert_called_once_with(self.settings, interactive=False)
        self.assertEqual(prepare.call_count, 2)
        upload.assert_called_once_with(videos[0], self.settings, job_id=job_id)
        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")

    def test_after_all_defers_postprocessing_until_every_download_finishes(self):
        self.settings["batch_postprocess_mode"] = "after_all"
        job_id = self.create_job(
            [
                "https://www.twitch.tv/videos/1234567890",
                "https://www.twitch.tv/videos/2345678901",
            ]
        )
        events = []
        video = self.root / "new.mp4"
        dependencies, process_calls, _ = self.dependencies(
            returncodes=[0, 0],
            candidates=[video],
            prepare=mock.Mock(side_effect=lambda path, *_args, **_kwargs: events.append("prepare") or path),
            mode="after_all",
        )
        original_popen = dependencies.popen

        def recording_popen(*args, **kwargs):
            events.append("download")
            return original_popen(*args, **kwargs)

        dependencies = DownloadWorkerDependencies(
            **{**dependencies.__dict__, "popen": recording_popen}
        )

        run_download_job(job_id, self.manager, dependencies)

        self.assertEqual(len(process_calls), 2)
        self.assertEqual(events, ["download", "download", "prepare", "prepare"])
        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(
            self.manager.jobs[job_id]["item_statuses"], ["fertig", "fertig"]
        )

    def test_missing_subprocess_sets_not_found_returncode(self):
        job_id = self.create_job()
        dependencies, _, _ = self.dependencies()
        dependencies = DownloadWorkerDependencies(
            **{
                **dependencies.__dict__,
                "popen": mock.Mock(side_effect=FileNotFoundError()),
            }
        )

        run_download_job(job_id, self.manager, dependencies)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], -1)
        self.assertEqual(self.manager.jobs[job_id]["item_statuses"], ["fehler"])


class UploadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = JobManager()
        self.settings = {
            "youtube_enabled": True,
            "youtube_privacy_status": "private",
            "youtube_playlist_id": "playlist-1",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_worker(self, paths, *, safe=None, service=None, upload=None):
        job_id = self.manager.create_upload_job([str(path) for path in paths], "Upload")
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            get_youtube_service=service or mock.Mock(),
            safe_local_video_path=safe or mock.Mock(side_effect=lambda raw, settings: Path(raw)),
            upload_to_youtube=upload or mock.Mock(return_value="video-id"),
        )
        run_upload_job(job_id, self.manager, dependencies)
        return job_id, dependencies

    def test_single_file_upload_success(self):
        video = self.root / "one.mp4"
        upload = mock.Mock(return_value="id-1")

        job_id, _ = self.run_worker([video], upload=upload)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 0)
        upload.assert_called_once_with(video, self.settings, job_id=job_id)

    def test_multi_file_upload_success(self):
        paths = [self.root / "one.mp4", self.root / "two.mp4"]
        upload = mock.Mock(side_effect=["id-1", "id-2"])

        job_id, dependencies = self.run_worker(paths, upload=upload)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 0)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            upload.call_args_list,
            [
                mock.call(paths[0], self.settings, job_id=job_id),
                mock.call(paths[1], self.settings, job_id=job_id),
            ],
        )
        self.assertEqual(dependencies.safe_local_video_path.call_count, 2)
        self.assertIn(
            "Local upload completed: 2 successful, 0 failed.",
            self.manager.jobs[job_id]["log"],
        )

    def test_one_upload_failure_continues_with_remaining_files(self):
        paths = [self.root / "one.mp4", self.root / "two.mp4"]
        upload = mock.Mock(side_effect=[RuntimeError("first failed"), "id-2"])

        job_id, _ = self.run_worker(paths, upload=upload)

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 1)
        self.assertIn(
            "Local upload completed: 1 successful, 1 failed.",
            self.manager.jobs[job_id]["log"],
        )

    def test_all_uploads_failing_preserves_partial_failure_result(self):
        paths = [self.root / "one.mp4", self.root / "two.mp4"]
        upload = mock.Mock(side_effect=RuntimeError("failed"))

        job_id, _ = self.run_worker(paths, upload=upload)

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 1)
        self.assertIn(
            "Local upload completed: 0 successful, 2 failed.",
            self.manager.jobs[job_id]["log"],
        )

    def test_each_queued_path_is_revalidated_before_upload(self):
        outside = self.root / "outside.mp4"
        safe = mock.Mock(side_effect=RuntimeError("outside media root"))
        upload = mock.Mock()

        job_id, _ = self.run_worker([outside], safe=safe, upload=upload)

        safe.assert_called_once_with(str(outside), self.settings)
        upload.assert_not_called()
        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 1)

    def test_connection_failure_prevents_upload_and_sets_worker_error(self):
        video = self.root / "one.mp4"
        upload = mock.Mock()

        job_id, _ = self.run_worker(
            [video],
            service=mock.Mock(side_effect=RuntimeError("not connected")),
            upload=upload,
        )

        upload.assert_not_called()
        self.assertEqual(self.manager.jobs[job_id]["status"], "fehler")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], -2)
        self.assertIn(
            "Local YouTube upload did not start: not connected",
            self.manager.jobs[job_id]["log"],
        )


_IMPORT_TMP = None
_OLD_ENV = {}
if "app" not in sys.modules:
    _IMPORT_TMP = tempfile.TemporaryDirectory()
    import_base = Path(_IMPORT_TMP.name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
        "VOD_DASHBOARD_LEGACY_SETTINGS_PATH",
    ):
        _OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(import_base / "data" / "settings.json")
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)

import app as dashboard  # noqa: E402


def tearDownModule():
    if _IMPORT_TMP is not None:
        _IMPORT_TMP.cleanup()
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AppJobCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.old_testing = dashboard.app.config.get("TESTING")
        self.old_auth_disabled = dashboard.app.config.get("VOD_AUTH_DISABLED")
        dashboard.app.config.update(TESTING=True, VOD_AUTH_DISABLED=True)
        with dashboard.job_lock:
            dashboard.jobs.clear()
            dashboard.job_counter = 0

    def tearDown(self):
        with dashboard.job_lock:
            dashboard.jobs.clear()
            dashboard.job_counter = 0
        dashboard.app.config["VOD_AUTH_DISABLED"] = self.old_auth_disabled
        dashboard.app.config["TESTING"] = self.old_testing

    def test_default_app_globals_alias_the_manager_state(self):
        self.assertIs(dashboard.jobs, dashboard.JOB_MANAGER.jobs)
        self.assertIs(dashboard.job_lock, dashboard.JOB_MANAGER.lock)

        with mock.patch.object(dashboard.threading, "Thread"):
            job_id = dashboard.create_job([], "Compatibility")
        with mock.patch.object(dashboard, "log_line") as logger:
            dashboard.append_job_log(job_id, "ready\n")

        self.assertEqual(job_id, "1")
        self.assertEqual(dashboard.job_counter, 1)
        self.assertEqual(dashboard.jobs[job_id]["log"], ["ready"])
        logger.assert_called_once_with("Job 1: ready")

    def test_patched_app_registry_lock_and_counter_are_honored(self):
        patched_jobs = {}
        patched_lock = threading.Lock()

        with (
            mock.patch.object(dashboard, "jobs", patched_jobs),
            mock.patch.object(dashboard, "job_lock", patched_lock),
            mock.patch.object(dashboard, "job_counter", 40),
            mock.patch.object(dashboard.threading, "Thread"),
            mock.patch.object(dashboard, "log_line"),
        ):
            job_id = dashboard.create_job([], "Patched")
            dashboard.append_job_log(job_id, "works")
            self.assertEqual(dashboard.job_counter, 41)

        self.assertEqual(job_id, "41")
        self.assertEqual(patched_jobs["41"]["log"], ["works"])

    def test_api_jobs_delegates_and_preserves_newest_first_contract(self):
        payload = [{"id": "2", "status": "wartet", "log": []}]
        client = dashboard.app.test_client()

        with mock.patch.object(
            dashboard.JOB_MANAGER, "snapshot_jobs", return_value=payload
        ) as snapshots:
            response = client.get("/api/jobs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"jobs": payload})
        snapshots.assert_called_once_with(reverse=True)

    def test_worker_wrappers_delegate_with_current_patchable_app_helpers(self):
        dashboard.jobs["download"] = {
            "id": "download",
            "status": "wartet",
            "urls": ["https://www.twitch.tv/videos/1234567890"],
            "log": [],
            "returncode": None,
        }
        dashboard.jobs["upload"] = {
            "id": "upload",
            "status": "wartet",
            "urls": ["C:/media/video.mp4"],
            "log": [],
            "returncode": None,
        }

        with mock.patch.object(
            dashboard, "load_settings", autospec=True
        ) as settings_loader, mock.patch.object(
            dashboard, "upload_video_to_youtube", autospec=True
        ) as uploader, mock.patch.object(
            dashboard.dashboard_jobs, "run_download_job"
        ) as download_worker, mock.patch.object(
            dashboard.dashboard_jobs, "run_upload_job"
        ) as upload_worker:
            dashboard.run_download_job("download")
            dashboard.run_upload_job("upload")

        download_args = download_worker.call_args.args
        upload_args = upload_worker.call_args.args
        self.assertEqual(download_args[0], "download")
        self.assertIs(download_args[1], dashboard.JOB_MANAGER)
        self.assertIs(download_args[2].load_settings, settings_loader)
        self.assertIs(download_args[2].upload_to_youtube, uploader)
        self.assertEqual(upload_args[0], "upload")
        self.assertIs(upload_args[1], dashboard.JOB_MANAGER)
        self.assertIs(upload_args[2].load_settings, settings_loader)
        self.assertIs(upload_args[2].upload_to_youtube, uploader)

    def test_download_and_upload_routes_keep_existing_response_contracts(self):
        client = dashboard.app.test_client()
        csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        with mock.patch.object(dashboard, "create_job", return_value="download-1"):
            download_response = client.post(
                "/api/download",
                json={"url": "https://www.twitch.tv/videos/1234567890"},
                headers=headers,
            )
        with mock.patch.object(
            dashboard, "create_upload_job", return_value="upload-1"
        ) as create_upload:
            upload_response = client.post(
                "/api/youtube/upload-local",
                json={"paths": ["C:/media/one.mp4", "C:/media/two.mp4"]},
                headers=headers,
            )

        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(
            download_response.get_json(),
            {
                "ok": True,
                "job_id": "download-1",
                "urls": ["https://www.twitch.tv/videos/1234567890"],
                "url_count": 1,
                "label": "Single VOD 1234567890",
            },
        )
        self.assertEqual(upload_response.status_code, 200)
        self.assertEqual(upload_response.get_json(), {"job_id": "upload-1"})
        create_upload.assert_called_once_with(
            ["C:/media/one.mp4", "C:/media/two.mp4"]
        )


if __name__ == "__main__":
    unittest.main()
