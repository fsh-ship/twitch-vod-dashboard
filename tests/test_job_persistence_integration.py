import json
import subprocess
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import (
    DownloadWorkerDependencies,
    JobManager,
    JobPersistenceRequiredError,
    RecordingWorkerDependencies,
    UploadWorkerDependencies,
    run_download_job,
    run_recording_job,
    run_upload_job,
)


UTC_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


class SpyStore:
    def __init__(self, events=None):
        self.calls = []
        self.events = events if events is not None else []
        self.fail = False
        self.manager = None
        self.lock_was_free = []

    def save(self, jobs, next_job_id, revision, *, media_root=None):
        if self.manager is not None:
            acquired = self.manager.lock.acquire(blocking=False)
            self.lock_was_free.append(acquired)
            if acquired:
                self.manager.lock.release()
        states = [job.get("state") for job in jobs]
        self.events.append(("save", revision, states))
        self.calls.append(
            {
                "jobs": deepcopy(jobs),
                "next_job_id": next_job_id,
                "revision": revision,
                "media_root": media_root,
            }
        )
        if self.fail:
            raise JobStorePersistenceError("private disk detail")
        return SimpleNamespace(revision=revision)

    def status(self):
        revision = self.calls[-1]["revision"] if self.calls else -1
        return {
            "last_save_at": "2026-08-23T12:00:00Z",
            "last_written_revision": revision,
        }


class FakeProcess:
    def __init__(self, lines=(), returncode=0):
        self.stdout = list(lines)
        self.returncode = returncode
        self.pid = 1234

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def upload_metadata(name="vod.mp4"):
    return {
        "streamer": "nika_livetv",
        "date": "2026-08-23",
        "title": "Title",
        "vod_id": "1234567890",
        "name": name,
        "size_bytes": 100,
        "size_gb": 0.1,
        "youtube_playlist_id": "",
    }


class JobPersistenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, store=None, *, clock=None):
        manager = JobManager(
            job_store=store,
            media_root=self.media_root,
            now=lambda: UTC_NOW,
            clock=clock,
        )
        if isinstance(store, SpyStore):
            store.manager = manager
        return manager

    def create_download(self, manager):
        return manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Download"
        )

    def create_upload(self, manager, name="vod.mp4"):
        path = self.media_root / name
        path.touch()
        return manager.create_upload_job(
            [str(path)], "Upload", item_metadata=[upload_metadata(name)]
        )

    def create_recording(self, manager):
        return manager.create_recording_job(
            "nika_livetv",
            stream_id="987654321",
            live_started_at="2026-08-23T11:00:00Z",
            output_name="nika_livetv/live.%(ext)s",
            origin="auto",
            attempt=2,
        )

    def download_dependencies(self, events, popen):
        return DownloadWorkerDependencies(
            load_settings=lambda: {
                "youtube_enabled": False,
                "youtube_auto_upload": False,
            },
            clean_postprocess_mode=lambda value: "per_vod",
            clean_rate_limit=lambda value: "",
            append_log=lambda job_id, text: None,
            snapshot_video_files=lambda settings: {},
            new_video_files=lambda before, after: [],
            recently_changed_video_files=lambda *args, **kwargs: [],
            prepare_manual_upload=lambda path, settings, job_id=None: path,
            get_youtube_service=lambda *args, **kwargs: None,
            upload_to_youtube=lambda *args, **kwargs: None,
            build_download_command=lambda urls, settings: (
                ["yt-dlp"],
                self.root / "urls.txt",
            ),
            download_directory=lambda settings: self.media_root,
            popen=popen,
            clock=lambda: 0.0,
        )

    def recording_dependencies(self, popen):
        return RecordingWorkerDependencies(
            load_settings=lambda: {},
            append_log=lambda job_id, text: None,
            build_recording_command=lambda *args, **kwargs: ["yt-dlp"],
            download_directory=lambda settings: self.media_root,
            resolve_completed_output=lambda raw, settings: str(raw),
            output_marker="OUTPUT=",
            popen=popen,
        )

    def test_storeless_manager_remains_process_local(self):
        manager = self.manager()
        job_id = self.create_download(manager)

        self.assertEqual(manager.jobs[job_id]["state"], "queued")
        self.assertEqual(
            manager.persistence_status(),
            {
                "enabled": False,
                "healthy": None,
                "last_save_at": None,
                "last_successful_revision": None,
                "last_error_code": "",
                "load_degraded": False,
                "load_source": "",
                "load_reason": "",
            },
        )

    def test_all_job_types_persist_complete_queued_state_at_creation(self):
        store = SpyStore()
        manager = self.manager(store)

        download_id = self.create_download(manager)
        manager.finish_claimed_item(download_id, "1-item-1", "cancelled")
        upload_id = self.create_upload(manager)
        manager.finish_claimed_item(upload_id, "2-item-1", "cancelled")
        recording_id = self.create_recording(manager)

        creations = [store.calls[0], store.calls[2], store.calls[4]]
        self.assertEqual(
            [call["jobs"][-1]["id"] for call in creations],
            [download_id, upload_id, recording_id],
        )
        for call in creations:
            job = call["jobs"][-1]
            self.assertEqual(job["state"], "queued")
            self.assertEqual(job["created_at"], "2026-08-23T12:00:00Z")
            self.assertEqual(job["updated_at"], job["created_at"])
            self.assertIsNone(job["started_at"])
            self.assertIsNone(job["finished_at"])

    def test_actual_store_serializes_each_job_type_immediately(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: UTC_NOW)
        manager = self.manager(store)

        self.create_download(manager)
        self.create_upload(manager)
        self.create_recording(manager)

        persisted = json.loads((self.root / "jobs.json").read_text("utf-8"))
        self.assertEqual(
            [job["type"] for job in persisted["jobs"]],
            ["download", "youtube_upload", "recording"],
        )
        self.assertEqual(persisted["next_job_id"], 4)

    def test_creation_failure_consumes_id_and_cannot_start_worker(self):
        store = SpyStore()
        manager = self.manager(store)
        store.fail = True
        thread_factory = mock.Mock()

        with self.assertRaises(JobPersistenceRequiredError) as raised:
            self.create_download(manager)
        self.assertEqual(raised.exception.code, "persistence_unavailable")
        self.assertEqual(manager.jobs["1"]["state"], "failed")
        thread_factory.assert_not_called()

        store.fail = False
        self.assertEqual(self.create_download(manager), "2")
        self.assertEqual(store.calls[-1]["next_job_id"], 3)

    def test_worker_thread_is_created_only_after_queued_save(self):
        events = []
        store = SpyStore(events)
        manager = self.manager(store)
        job_id = self.create_download(manager)

        class Thread:
            def __init__(self, **kwargs):
                events.append(("thread_created",))

            def start(self):
                events.append(("thread_started",))

        manager.start_worker(lambda value: None, job_id, thread_factory=Thread)
        self.assertEqual(events[0][0], "save")
        self.assertEqual(events[1:], [("thread_created",), ("thread_started",)])

    def test_download_running_save_precedes_popen_and_failure_blocks_it(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                events = []
                store = SpyStore(events)
                manager = self.manager(store)
                job_id = self.create_download(manager)
                store.fail = fail

                def popen(*args, **kwargs):
                    events.append(("popen",))
                    return FakeProcess()

                run_download_job(
                    job_id,
                    manager,
                    self.download_dependencies(events, popen),
                )
                running_save = next(
                    index
                    for index, event in enumerate(events)
                    if event[0] == "save" and "running" in event[2]
                )
                if fail:
                    self.assertNotIn(("popen",), events)
                    self.assertEqual(manager.jobs[job_id]["state"], "failed")
                else:
                    self.assertLess(running_save, events.index(("popen",)))

    def test_upload_running_save_precedes_all_remote_calls(self):
        events = []
        store = SpyStore(events)
        manager = self.manager(store)
        job_id = self.create_upload(manager)
        service = mock.Mock(side_effect=lambda *a, **k: events.append(("service",)))
        upload = mock.Mock(side_effect=lambda *a, **k: events.append(("upload",)) or "video")
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: {"youtube_enabled": True},
            append_log=lambda job_id, text: None,
            get_youtube_service=service,
            safe_local_video_path=lambda raw, settings: Path(raw),
            upload_to_youtube=upload,
        )

        run_upload_job(job_id, manager, dependencies)

        running_save = next(
            index
            for index, event in enumerate(events)
            if event[0] == "save" and "running" in event[2]
        )
        self.assertLess(running_save, events.index(("service",)))
        self.assertLess(running_save, events.index(("upload",)))

    def test_required_upload_save_failure_has_zero_remote_side_effect(self):
        store = SpyStore()
        manager = self.manager(store)
        job_id = self.create_upload(manager)
        store.fail = True
        service = mock.Mock()
        upload = mock.Mock()

        run_upload_job(
            job_id,
            manager,
            UploadWorkerDependencies(
                load_settings=lambda: {"youtube_enabled": True},
                append_log=lambda job_id, text: None,
                get_youtube_service=service,
                safe_local_video_path=lambda raw, settings: Path(raw),
                upload_to_youtube=upload,
            ),
        )

        service.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(manager.jobs[job_id]["state"], "failed")

    def test_upload_setup_failure_releases_lane_for_the_next_job(self):
        manager = self.manager()
        first = self.create_upload(manager, "first.mp4")
        second = self.create_upload(manager, "second.mp4")
        failed_dependencies = UploadWorkerDependencies(
            load_settings=lambda: {"youtube_enabled": True},
            append_log=lambda job_id, text: None,
            get_youtube_service=mock.Mock(
                side_effect=RuntimeError("connection failed")
            ),
            safe_local_video_path=lambda raw, settings: Path(raw),
            upload_to_youtube=mock.Mock(),
        )

        run_upload_job(first, manager, failed_dependencies)

        self.assertFalse(
            manager.queue_controls_snapshot()["youtube_upload"][
                "has_active_item"
            ]
        )
        upload = mock.Mock(return_value="video")
        run_upload_job(
            second,
            manager,
            UploadWorkerDependencies(
                load_settings=lambda: {"youtube_enabled": True},
                append_log=lambda job_id, text: None,
                get_youtube_service=mock.Mock(),
                safe_local_video_path=lambda raw, settings: Path(raw),
                upload_to_youtube=upload,
            ),
        )
        upload.assert_called_once()
        self.assertEqual(manager.jobs[second]["state"], "completed")

    def test_recording_running_save_precedes_popen_and_failure_blocks_it(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                events = []
                store = SpyStore(events)
                manager = self.manager(store)
                job_id = self.create_recording(manager)
                store.fail = fail

                def popen(*args, **kwargs):
                    events.append(("popen",))
                    return FakeProcess(lines=["OUTPUT=nika_livetv/vod.mp4\n"])

                run_recording_job(
                    job_id, manager, self.recording_dependencies(popen)
                )
                running_save = next(
                    index
                    for index, event in enumerate(events)
                    if event[0] == "save"
                    and any(
                        state in {"running", "stopping"}
                        for state in event[2]
                    )
                )
                if fail:
                    self.assertNotIn(("popen",), events)
                    self.assertEqual(manager.jobs[job_id]["state"], "failed")
                else:
                    self.assertLess(running_save, events.index(("popen",)))
                    persisted = store.calls[-1]["jobs"][-1]
                    self.assertEqual(persisted["state"], "completed")
                    self.assertEqual(
                        persisted["completion_reason"], "natural_end"
                    )
                    self.assertEqual(
                        persisted["output_path"], "nika_livetv/vod.mp4"
                    )
                    self.assertTrue(persisted["output_complete"])

    def test_progress_is_throttled_but_lifecycle_and_terminal_are_immediate(self):
        now = [0.0]
        store = SpyStore()
        manager = self.manager(store, clock=lambda: now[0])
        job_id = self.create_upload(manager)
        item_id = manager.claim_next_item(job_id)["item_id"]
        baseline = len(store.calls)

        self.assertTrue(
            manager.update_active_upload_progress(
                job_id, 10, 100, item_id=item_id
            )
        )
        self.assertEqual(len(store.calls), baseline + 1)
        now[0] = 30.0
        manager.update_active_upload_progress(job_id, 20, 100, item_id=item_id)
        self.assertEqual(len(store.calls), baseline + 1)
        now[0] = 60.0
        manager.update_active_upload_progress(job_id, 30, 100, item_id=item_id)
        self.assertEqual(len(store.calls), baseline + 2)
        now[0] = 61.0
        manager.finish_claimed_item(job_id, item_id, "completed")
        self.assertEqual(len(store.calls), baseline + 3)
        self.assertIsNotNone(manager.jobs[job_id]["finished_at"])

    def test_batch_finished_at_waits_until_every_item_is_terminal(self):
        store = SpyStore()
        manager = self.manager(store)
        job_id = manager.create_download_job(
            [
                "https://www.twitch.tv/videos/1234567890",
                "https://www.twitch.tv/videos/2345678901",
            ],
            "Batch",
        )

        first = manager.claim_next_item(job_id)
        manager.finish_claimed_item(job_id, first["item_id"], "completed")
        self.assertIsNone(manager.jobs[job_id]["finished_at"])
        second = manager.claim_next_item(job_id)
        manager.finish_claimed_item(job_id, second["item_id"], "completed")
        finished_at = manager.jobs[job_id]["finished_at"]
        self.assertEqual(finished_at, "2026-08-23T12:00:00Z")
        manager.finish_job(job_id, 0, "fertig")
        self.assertEqual(manager.jobs[job_id]["finished_at"], finished_at)

    def test_failed_download_and_recording_are_persisted_immediately(self):
        store = SpyStore()
        manager = self.manager(store)
        download = self.create_download(manager)
        download_item = manager.claim_next_item(download)["item_id"]
        before_download_finish = len(store.calls)

        manager.finish_claimed_item(
            download, download_item, "failed", failure_kind="known"
        )

        self.assertEqual(len(store.calls), before_download_finish + 1)
        self.assertEqual(store.calls[-1]["jobs"][-1]["state"], "failed")
        manager.finish_job(download, 1, "fehler")

        recording = self.create_recording(manager)
        recording_item = manager.claim_recording_job(recording)["item_id"]
        before_recording_finish = len(store.calls)
        manager.finalize_recording_job(
            recording,
            recording_item,
            state="failed",
            returncode=1,
            completion_reason="process_error",
        )
        self.assertEqual(len(store.calls), before_recording_finish + 1)
        persisted = store.calls[-1]["jobs"][-1]
        self.assertEqual(persisted["state"], "failed")
        self.assertEqual(persisted["completion_reason"], "process_error")

    def test_log_only_does_not_save_but_structured_download_progress_does(self):
        store = SpyStore()
        manager = self.manager(store, clock=lambda: 0.0)
        job_id = self.create_download(manager)
        manager.claim_next_item(job_id)
        baseline = len(store.calls)

        manager.append_job_log(job_id, "ordinary diagnostic")
        self.assertEqual(len(store.calls), baseline)
        manager.append_job_log(
            job_id, "[download] 10.0% of 1GiB at 2MiB/s ETA 00:30"
        )
        self.assertEqual(len(store.calls), baseline + 1)

    def test_checkpoint_failure_degrades_health_without_stopping_active_work(self):
        for kind in ("download", "youtube_upload", "recording"):
            with self.subTest(kind=kind):
                store = SpyStore()
                manager = self.manager(store, clock=lambda: 0.0)
                if kind == "download":
                    job_id = self.create_download(manager)
                    item_id = manager.claim_next_item(job_id)["item_id"]
                    store.fail = True
                    manager.append_job_log(
                        job_id,
                        "[download] 10.0% of 1GiB at 2MiB/s ETA 00:30",
                    )
                elif kind == "youtube_upload":
                    job_id = self.create_upload(manager)
                    item_id = manager.claim_next_item(job_id)["item_id"]
                    store.fail = True
                    manager.update_active_upload_progress(
                        job_id, 10, 100, item_id=item_id
                    )
                else:
                    job_id = self.create_recording(manager)
                    item_id = manager.claim_recording_job(job_id)["item_id"]
                    store.fail = True
                    manager.update_recorded_seconds(job_id, 10)

                self.assertEqual(manager.item_state(job_id, item_id), "running")
                self.assertFalse(manager.persistence_status()["healthy"])
                store.fail = False
                if kind == "recording":
                    manager.finalize_recording_job(
                        job_id,
                        item_id,
                        state="completed",
                        returncode=0,
                        completion_reason="natural_end",
                        output_path="nika_livetv/vod.mp4",
                    )
                else:
                    manager.finish_claimed_item(job_id, item_id, "completed")
                self.assertTrue(manager.persistence_status()["healthy"])

    def test_cancel_intent_is_saved_before_runtime_event_is_set(self):
        store = SpyStore()
        manager = self.manager(store)
        job_id = self.create_upload(manager)
        item_id = manager.claim_next_item(job_id)["item_id"]

        lane = manager.request_cancel_item(job_id, item_id)

        self.assertEqual(lane, "youtube_upload")
        self.assertEqual(store.calls[-1]["jobs"][-1]["state"], "running")
        self.assertEqual(
            store.calls[-1]["jobs"][-1]["item_states"], ["cancelling"]
        )
        self.assertTrue(manager.is_cancel_requested(job_id, item_id))

    def test_media_path_is_relative_and_outside_path_blocks_creation(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: UTC_NOW)
        manager = self.manager(store)
        inside = self.media_root / "inside.mp4"
        inside.touch()

        manager.create_upload_job(
            [str(inside)],
            "Inside",
            item_metadata=[upload_metadata("inside.mp4")],
        )
        persisted = json.loads((self.root / "jobs.json").read_text("utf-8"))
        self.assertEqual(persisted["jobs"][0]["urls"], ["inside.mp4"])
        self.assertNotIn(str(self.media_root), json.dumps(persisted))

        outside = self.root / "outside.mp4"
        outside.touch()
        with self.assertRaises(JobPersistenceRequiredError) as raised:
            manager.create_upload_job(
                [str(outside)],
                "Outside",
                item_metadata=[upload_metadata("outside.mp4")],
            )
        self.assertEqual(
            raised.exception.code, "persistence_validation_failed"
        )
        self.assertNotIn("2", manager.jobs)
        safe = self.media_root / "safe-after-rejection.mp4"
        safe.touch()
        next_id = manager.create_upload_job(
            [str(safe)],
            "Safe",
            item_metadata=[upload_metadata(safe.name)],
        )
        self.assertEqual(next_id, "3")

    def test_durable_upload_failure_excludes_raw_error_and_runtime_fields(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: UTC_NOW)
        manager = self.manager(store)
        job_id = self.create_upload(manager)
        item_id = manager.claim_next_item(job_id)["item_id"]
        raw_error = "secret token at C:/private/account.json"

        manager.set_upload_item_status(
            job_id, 1, "fehler", error=raw_error
        )
        manager.finish_claimed_item(
            job_id, item_id, "failed", failure_kind="known"
        )

        text = (self.root / "jobs.json").read_text("utf-8")
        persisted = json.loads(text)["jobs"][0]
        self.assertNotIn(raw_error, text)
        self.assertNotIn("item_errors", persisted)
        self.assertNotIn("log", persisted)
        self.assertNotIn("item_bytes_per_second", persisted)
        self.assertNotIn("item_eta_seconds", persisted)
        self.assertEqual(persisted["item_failure_kinds"], ["known"])

    def test_retry_relationship_and_recording_stop_intent_save_immediately(self):
        store = SpyStore()
        manager = self.manager(store)
        original = self.create_download(manager)
        item_id = manager.claim_next_item(original)["item_id"]
        manager.finish_claimed_item(original, item_id, "failed")
        retry = self.create_download(manager)
        manager.update_job(
            retry, retry_of={"job_id": original, "item_id": item_id}
        )
        manager.finalize_retry(original, item_id, retry)
        self.assertEqual(
            store.calls[-1]["jobs"][0]["item_retry_job_ids"], [retry]
        )

        manager.finish_claimed_item(retry, "2-item-1", "cancelled")
        recording = self.create_recording(manager)
        manager.claim_recording_job(recording)
        manager.request_recording_stop(recording)
        saved = store.calls[-1]["jobs"][-1]
        self.assertTrue(saved["stop_requested"])
        self.assertEqual(saved["state"], "stopping")

    def test_writes_happen_outside_manager_lock_and_revisions_are_unique(self):
        store = SpyStore()
        manager = self.manager(store)
        job_id = self.create_download(manager)

        threads = [
            threading.Thread(
                target=manager.update_job,
                args=(job_id,),
                kwargs={"label": f"Download {index}"},
            )
            for index in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        revisions = [call["revision"] for call in store.calls]
        self.assertEqual(len(revisions), len(set(revisions)))
        self.assertTrue(all(store.lock_was_free))

    def test_stale_running_snapshot_cannot_overwrite_terminal_state(self):
        store = JobStore(self.root / "jobs.json", clock=lambda: UTC_NOW)
        manager = self.manager(store)
        job_id = self.create_download(manager)
        item_id = manager.claim_next_item(job_id)["item_id"]
        with manager.lock:
            stale = manager._snapshot_for_persistence_locked()

        manager.finish_claimed_item(job_id, item_id, "completed")
        manager._persist_best_effort(stale)

        persisted = json.loads((self.root / "jobs.json").read_text("utf-8"))
        self.assertEqual(persisted["jobs"][0]["state"], "completed")
        self.assertEqual(persisted["jobs"][0]["item_states"], ["completed"])

    def test_persistent_compatible_manager_is_reused_to_avoid_store_races(self):
        manager = self.manager(SpyStore())
        alternate = JobManager.compatible_with(
            manager, {}, threading.Lock(), 99
        )
        self.assertIs(alternate, manager)


if __name__ == "__main__":
    unittest.main()
