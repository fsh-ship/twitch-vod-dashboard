import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


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
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(
        import_base / "data" / "settings.json"
    )
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)

import app as dashboard
from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import JobManager, JobPersistenceRequiredError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def tearDownModule():
    if _IMPORT_TMP is not None:
        _IMPORT_TMP.cleanup()
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def upload_metadata(name: str) -> dict:
    return {
        "streamer": "nika_livetv",
        "date": "2026-08-23",
        "title": "Runtime test",
        "vod_id": "1234567890",
        "name": name,
        "size_bytes": 4,
        "size_gb": 0.0,
        "youtube_playlist_id": "",
    }


class FakeMonitor:
    def __init__(self, events=None, *, fail_start=False):
        self.events = events if events is not None else []
        self.fail_start = fail_start
        self.prepare_calls = 0
        self.start_calls = 0

    def prepare_after_restart(self):
        self.prepare_calls += 1
        self.events.append("prepare")
        return {"state_healthy": True}

    def start(self):
        self.start_calls += 1
        self.events.append("monitor")
        if self.fail_start:
            raise RuntimeError("private monitor detail")
        return True


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.media = self.root / "media"
        self.media.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def runtime_context(self, manager, monitor, *, store_factory=None):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(dashboard, "JOB_MANAGER", manager))
        stack.enter_context(mock.patch.object(dashboard, "jobs", manager.jobs))
        stack.enter_context(mock.patch.object(dashboard, "job_lock", manager.lock))
        stack.enter_context(mock.patch.object(dashboard, "job_counter", 0))
        stack.enter_context(
            mock.patch.object(dashboard, "DEFAULT_DASHBOARD_DIR", self.data)
        )
        stack.enter_context(mock.patch.object(dashboard, "MEDIA_ROOT", self.media))
        stack.enter_context(
            mock.patch.object(dashboard, "WORKER_RUNTIME_RESULT", None)
        )
        stack.enter_context(
            mock.patch.object(dashboard, "AUTO_RECORDER_MONITOR", None)
        )
        stack.enter_context(
            mock.patch.object(
                dashboard, "create_auto_recorder_monitor", return_value=monitor
            )
        )
        if store_factory is not None:
            stack.enter_context(
                mock.patch.object(
                    dashboard.dashboard_job_store.JobStore,
                    "from_dashboard_dir",
                    side_effect=store_factory,
                )
            )
        return stack

    def initialize(self, manager=None, monitor=None, *, store_factory=None):
        manager = manager or JobManager()
        monitor = monitor or FakeMonitor()
        context = self.runtime_context(
            manager, monitor, store_factory=store_factory
        )
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        result = dashboard.initialize_worker_runtime(worker_count=1)
        return manager, monitor, result

    def test_import_is_side_effect_free(self):
        data = self.root / "import-data"
        media = self.root / "import-media"
        environment = dict(os.environ)
        environment.update(
            {
                "VOD_DASHBOARD_DIR": str(data),
                "VOD_DASHBOARD_MEDIA_ROOT": str(media),
                "VOD_DASHBOARD_SETTINGS": str(data / "settings.json"),
                "VOD_DASHBOARD_AUTH_DISABLED": "1",
            }
        )
        code = (
            "import json, pathlib, threading, app; "
            "print(json.dumps({'jobs': pathlib.Path(app.DEFAULT_DASHBOARD_DIR, "
            "'jobs.json').exists(), 'monitor': app.AUTO_RECORDER_MONITOR is not None, "
            "'threads': [t.name for t in threading.enumerate()]}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["jobs"])
        self.assertFalse(payload["monitor"])
        self.assertNotIn("auto-recorder-monitor", payload["threads"])

    def test_missing_store_order_idempotency_and_first_mutation(self):
        events = []
        manager = JobManager()
        monitor = FakeMonitor(events)
        configure_original = manager.configure_persistence
        restore_original = manager.restore_from_store

        def configure_side_effect(*args, **kwargs):
            events.append("configure")
            return configure_original(*args, **kwargs)

        def restore_side_effect(*args, **kwargs):
            events.append("restore")
            return restore_original(*args, **kwargs)

        with self.runtime_context(manager, monitor), mock.patch.object(
            manager,
            "configure_persistence",
            side_effect=configure_side_effect,
        ) as configure, mock.patch.object(
            manager, "restore_from_store", side_effect=restore_side_effect
        ) as restore:
            first = dashboard.initialize_worker_runtime(worker_count=1)
            second = dashboard.initialize_worker_runtime(worker_count=1)

            self.assertEqual(first, second)
            self.assertTrue(first["usable"])
            self.assertFalse(first["degraded"])
            self.assertEqual(first["reason"], "missing")
            self.assertEqual(
                events, ["configure", "restore", "prepare", "monitor"]
            )
            configure.assert_called_once()
            restore.assert_called_once()
            self.assertEqual(monitor.prepare_calls, 1)
            self.assertEqual(monitor.start_calls, 1)
            self.assertEqual(manager.job_store.path, self.data / "jobs.json")
            self.assertFalse((self.data / "jobs.json").exists())
            self.assertEqual(
                manager.persistence_status()["healthy"], True
            )
            with dashboard.app.test_client() as client:
                persistence = client.get("/api/jobs").get_json()[
                    "persistence_status"
                ]
            self.assertEqual(
                persistence,
                {
                    "enabled": True,
                    "healthy": True,
                    "current_degraded": False,
                    "history_degraded": False,
                },
            )
            self.assertNotIn("path", persistence)

            job_id = manager.create_download_job(
                ["https://www.twitch.tv/videos/1234567890"], "Download"
            )
            self.assertEqual(job_id, "1")
            self.assertTrue((self.data / "jobs.json").exists())

    def test_startup_runs_auto_youtube_execution_reconciliation_after_restore(self):
        events = []
        manager = JobManager()
        monitor = FakeMonitor(events)
        restore_original = manager.restore_from_store
        execution = mock.Mock()
        execution.reconcile.side_effect = lambda: events.append(
            "execution_reconcile"
        ) or {
            "deferred": 0,
            "queued": 0,
            "confirmed": 1,
            "blocked": 0,
            "pending": 0,
        }

        def restore():
            events.append("restore")
            return restore_original()

        with self.runtime_context(manager, monitor), mock.patch.object(
            manager, "restore_from_store", side_effect=restore
        ), mock.patch.object(
            dashboard,
            "_auto_youtube_execution_service",
            return_value=execution,
        ) as factory:
            result = dashboard.initialize_worker_runtime(worker_count=1)

        self.assertTrue(result["initialized"])
        factory.assert_called_once_with(manager)
        execution.reconcile.assert_called_once_with()
        self.assertLess(events.index("restore"), events.index("execution_reconcile"))
        self.assertLess(events.index("execution_reconcile"), events.index("monitor"))

    def test_full_restart_reconciles_all_types_offline_and_keeps_ids(self):
        first = JobManager()
        with self.runtime_context(first, FakeMonitor()):
            dashboard.initialize_worker_runtime(worker_count=1)
            running_download = first.create_download_job(
                ["https://www.twitch.tv/videos/2345678901"], "Running download"
            )
            first.claim_next_item(running_download)
            first.update_job(
                running_download,
                item_progress=[37.5],
                item_processed_seconds=[900.0],
                item_total_duration_seconds=[1800.0],
            )
            queued_download = first.create_download_job(
                ["https://www.twitch.tv/videos/1234567890"], "Queued download"
            )

            queued_file = self.media / "queued.mp4"
            running_file = self.media / "running.mp4"
            queued_file.write_bytes(b"vod")
            running_file.write_bytes(b"vod")
            running_upload = first.create_upload_job(
                [str(running_file)],
                "Running upload",
                item_metadata=[upload_metadata(running_file.name)],
            )
            first.claim_next_item(running_upload)
            queued_upload = first.create_upload_job(
                [str(queued_file)],
                "Queued upload",
                item_metadata=[upload_metadata(queued_file.name)],
            )
            recording = first.create_recording_job(
                "nika_livetv",
                stream_id="987654321",
                output_name="nika_livetv/live.%(ext)s",
                origin="auto",
                attempt=1,
            )
            first.claim_recording_job(recording)
            first.update_recorded_seconds(recording, 123.5)
            first.update_job(
                running_download,
                raw_secret="sentinel-secret",
                command=["yt-dlp", "signed-url"],
            )
            self.assertTrue(first.flush_persistence())

        persisted_text = (self.data / "jobs.json").read_text("utf-8")
        self.assertNotIn("sentinel-secret", persisted_text)
        self.assertNotIn("signed-url", persisted_text)
        self.assertNotIn(str(self.media), persisted_text)

        second = JobManager()
        with self.runtime_context(second, FakeMonitor()):
            with mock.patch(
                "vod_dashboard.jobs.threading.Thread",
                side_effect=AssertionError("restore must not start workers"),
            ), mock.patch(
                "vod_dashboard.jobs.subprocess.Popen",
                side_effect=AssertionError("restore must not start processes"),
            ):
                result = dashboard.initialize_worker_runtime(worker_count=1)

            self.assertEqual(result["loaded_count"], 5)
            self.assertEqual(result["reconciled_item_count"], 5)
            self.assertEqual(
                second.get_job(queued_download)["item_recovery_reasons"],
                ["restart_before_start"],
            )
            self.assertEqual(
                second.get_job(running_download)["item_recovery_reasons"],
                ["restart_interrupted"],
            )
            self.assertEqual(
                second.get_job(running_download)["item_progress"], [37.5]
            )
            self.assertEqual(
                second.get_job(running_download)["item_processed_seconds"],
                [900.0],
            )
            self.assertEqual(
                second.get_job(queued_upload)["item_recovery_reasons"],
                ["restart_before_start"],
            )
            queued_upload_snapshot = next(
                job
                for job in second.snapshot_jobs()
                if job["id"] == queued_upload
            )
            self.assertTrue(
                queued_upload_snapshot["item_capabilities"][0]["can_retry"]
            )
            upload = second.get_job(running_upload)
            self.assertEqual(upload["item_recovery_reasons"], ["upload_status_unknown"])
            self.assertEqual(upload["item_failure_kinds"], ["uncertain"])
            upload_snapshot = next(
                job
                for job in second.snapshot_jobs()
                if job["id"] == running_upload
            )
            self.assertFalse(
                upload_snapshot["item_capabilities"][0]["can_retry"]
            )
            self.assertEqual(
                second.get_job(recording)["item_recovery_reasons"],
                ["restart_interrupted"],
            )
            self.assertFalse(second.get_job(recording)["output_complete"])
            self.assertEqual(second.get_job(recording)["recorded_seconds"], 123.5)
            self.assertEqual(second._download_processes, {})
            self.assertEqual(second._recording_processes, {})
            self.assertEqual(
                second.create_download_job(
                    ["https://www.twitch.tv/videos/3456789012"], "Next"
                ),
                "6",
            )

        third = JobManager()
        with self.runtime_context(third, FakeMonitor()):
            dashboard.initialize_worker_runtime(worker_count=1)
            self.assertEqual(
                third.create_download_job(
                    ["https://www.twitch.tv/videos/4567890123"], "After restart"
                ),
                "7",
            )

    def test_corrupt_store_is_preserved_and_blocks_new_side_effects(self):
        self.data.mkdir()
        path = self.data / "jobs.json"
        path.write_bytes(b"{not-json")
        before = path.read_bytes()
        manager = JobManager()
        with self.runtime_context(manager, FakeMonitor()) as _:
            result = dashboard.initialize_worker_runtime(worker_count=1)
            self.assertTrue(result["usable"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["reason"], "invalid_json")
            self.assertFalse(manager.persistence_status()["healthy"])
            with dashboard.app.test_client() as client:
                persistence = client.get("/api/jobs").get_json()[
                    "persistence_status"
                ]
            self.assertTrue(persistence["current_degraded"])
            self.assertTrue(persistence["history_degraded"])
            worker = mock.Mock()
            with mock.patch.object(manager, "start_worker", worker), self.assertRaises(
                JobPersistenceRequiredError
            ):
                dashboard.create_job(
                    ["https://www.twitch.tv/videos/1234567890"], "Blocked"
                )
            worker.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_store_construction_and_write_failures_are_fail_closed(self):
        manager, monitor, result = self.initialize(
            store_factory=RuntimeError("private construction detail")
        )
        self.assertTrue(result["usable"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "store_unavailable")
        self.assertEqual(monitor.start_calls, 1)
        with self.assertRaises(JobPersistenceRequiredError):
            manager.create_download_job(
                ["https://www.twitch.tv/videos/1234567890"], "Blocked"
            )

        class WriteFailingStore(JobStore):
            def save(self, *args, **kwargs):
                raise JobStorePersistenceError("private permission detail")

        second = JobManager()
        second_monitor = FakeMonitor()
        with self.runtime_context(
            second,
            second_monitor,
            store_factory=lambda _directory: WriteFailingStore(
                self.root / "unwritable" / "jobs.json"
            ),
        ):
            initialized = dashboard.initialize_worker_runtime(worker_count=1)
            self.assertTrue(initialized["usable"])
            with self.assertRaises(JobPersistenceRequiredError):
                second.create_download_job(
                    ["https://www.twitch.tv/videos/2345678901"], "Blocked"
                )
            self.assertFalse(second.persistence_status()["healthy"])

    def test_monitor_start_failure_does_not_undo_restored_jobs(self):
        source = JobManager(
            job_store=JobStore.from_dashboard_dir(self.data),
            media_root=self.media,
        )
        job_id = source.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "History"
        )
        source.remove_queued_item(job_id, f"{job_id}-item-1")

        manager = JobManager()
        monitor = FakeMonitor(fail_start=True)
        with self.runtime_context(manager, monitor):
            result = dashboard.initialize_worker_runtime(worker_count=1)
            self.assertTrue(result["usable"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["reason"], "monitor_start_failed")
            self.assertIsNotNone(manager.get_job(job_id))
            self.assertTrue(manager.persistence_status()["enabled"])

    def test_graceful_shutdown_stops_owned_download_and_flushes_terminals(self):
        manager = JobManager()
        with self.runtime_context(manager, FakeMonitor()):
            dashboard.initialize_worker_runtime(worker_count=1)
            download_id = manager.create_download_job(
                ["https://www.twitch.tv/videos/1234567890"], "Download"
            )
            download_claim = manager.claim_next_item(download_id)

            class Process:
                returncode = None

                def poll(self):
                    return self.returncode

            process = Process()
            manager.register_download_process(
                download_id, download_claim["item_id"], process
            )
            recording_id = manager.create_recording_job(
                "nika_livetv",
                output_name="nika_livetv/live.%(ext)s",
                origin="manual",
                attempt=1,
            )
            recording_claim = manager.claim_recording_job(recording_id)
            output = self.media / "nika_livetv" / "live.mp4"
            output.parent.mkdir()
            output.write_bytes(b"recording")
            manager.finalize_recording_job(
                recording_id,
                recording_claim["item_id"],
                state="completed",
                returncode=0,
                completion_reason="natural_end",
                output_path=str(output),
            )

            manager.begin_shutdown()

            def terminate(owned):
                self.assertIs(owned, process)
                owned.returncode = 130

            self.assertTrue(
                manager.stop_downloads_for_shutdown(terminator=terminate)
            )
            with manager.lock:
                manager.jobs[download_id]["label"] = "Flushed label"
                manager._mark_dirty_locked(manager.jobs[download_id])
            self.assertTrue(manager.flush_persistence())
            self.assertEqual(manager.get_job(download_id)["state"], "interrupted")
            self.assertEqual(
                manager.get_job(download_id)["item_recovery_reasons"],
                ["worker_shutdown"],
            )

        restored = JobManager()
        with self.runtime_context(restored, FakeMonitor()):
            result = dashboard.initialize_worker_runtime(worker_count=1)
            self.assertEqual(result["reconciled_item_count"], 0)
            self.assertEqual(restored.get_job(download_id)["label"], "Flushed label")
            recording = restored.get_job(recording_id)
            self.assertEqual(recording["state"], "completed")
            self.assertEqual(recording["completion_reason"], "natural_end")

    def test_shutdown_prevents_new_claims_without_touching_upload_processes(self):
        manager = JobManager()
        upload = self.media / "queued.mp4"
        upload.write_bytes(b"vod")
        download_id = manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Queued download"
        )
        upload_id = manager.create_upload_job([str(upload)], "Queued upload")
        manager.begin_shutdown()
        self.assertIsNone(manager.claim_next_item(download_id))
        self.assertIsNone(manager.claim_next_item(upload_id))
        self.assertEqual(manager.get_job(download_id)["state"], "queued")
        self.assertEqual(manager.get_job(upload_id)["state"], "queued")

    def test_single_worker_contract_is_fail_safe(self):
        manager = JobManager()
        monitor = FakeMonitor()
        with self.runtime_context(manager, monitor):
            result = dashboard.initialize_worker_runtime(worker_count=2)
            self.assertFalse(result["usable"])
            self.assertEqual(result["reason"], "unsupported_worker_count")
            self.assertIsNone(manager.job_store)
            self.assertEqual(monitor.start_calls, 0)

    def test_persistence_cannot_be_activated_after_process_local_work(self):
        manager = JobManager()
        manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Too early"
        )
        monitor = FakeMonitor()
        with self.runtime_context(manager, monitor):
            result = dashboard.initialize_worker_runtime(worker_count=1)
            self.assertFalse(result["usable"])
            self.assertEqual(
                result["reason"], "persistence_activation_too_late"
            )
            self.assertIsNone(manager.job_store)
            self.assertEqual(monitor.start_calls, 0)


if __name__ == "__main__":
    unittest.main()
