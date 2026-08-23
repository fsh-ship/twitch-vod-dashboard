import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


_IMPORT_TMP = None
_OLD_ENV = {}
if "app" not in sys.modules:
    _IMPORT_TMP = tempfile.TemporaryDirectory()
    import_root = Path(_IMPORT_TMP.name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
        "VOD_DASHBOARD_LEGACY_SETTINGS_PATH",
    ):
        _OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_root / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_root / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(
        import_root / "data" / "settings.json"
    )
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)

import app as dashboard  # noqa: E402
from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import JobManager


CREATED = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
RESTARTED = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)


class ToggleFailStore(JobStore):
    fail = False

    def save(self, *args, **kwargs):
        if self.fail:
            raise JobStorePersistenceError("private disk failure")
        return super().save(*args, **kwargs)


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.old_testing = dashboard.app.config.get("TESTING")
        self.old_auth_disabled = dashboard.app.config.get(
            "VOD_AUTH_DISABLED"
        )
        dashboard.app.config.update(TESTING=True, VOD_AUTH_DISABLED=True)

    def tearDown(self):
        dashboard.app.config["VOD_AUTH_DISABLED"] = self.old_auth_disabled
        dashboard.app.config["TESTING"] = self.old_testing
        self.temporary.cleanup()

    def store(self, name, *, toggle=False):
        store_type = ToggleFailStore if toggle else JobStore
        return store_type(self.root / name, clock=lambda: CREATED)

    def manager(self, store, *, restarted=False):
        return JobManager(
            job_store=store,
            media_root=self.media,
            now=lambda: RESTARTED if restarted else CREATED,
        )

    def restored_download(self, name, *, active=False, urls=None):
        store = self.store(name)
        source = self.manager(store)
        job_id = source.create_download_job(
            urls
            or ["https://www.twitch.tv/videos/1234567890"],
            "Original download batch",
        )
        if active:
            source.claim_next_item(job_id)
        restored = self.manager(store, restarted=True)
        restored.restore_from_store()
        return restored, store, job_id

    def upload_metadata(
        self,
        path,
        *,
        playlist="playlist-item",
        size=None,
    ):
        return {
            "streamer": "nika_livetv",
            "date": "2026-08-23",
            "title": "Frozen Twitch title",
            "vod_id": "1234567890",
            "name": path.name,
            "size_bytes": path.stat().st_size if size is None else size,
            "size_gb": 0.001,
            "youtube_playlist_id": playlist,
        }

    def restored_upload(self, name, *, active=False, path=None):
        path = path or self.media / f"{name}.mp4"
        path.write_bytes(b"original-video")
        store = self.store(name)
        source = self.manager(store)
        job_id = source.create_upload_job(
            [str(path)],
            "Original upload",
            playlist_id="playlist-job",
            item_metadata=[self.upload_metadata(path)],
        )
        if active:
            source.claim_next_item(job_id)
        restored = self.manager(store, restarted=True)
        restored.restore_from_store()
        return restored, store, job_id, path

    @contextmanager
    def app_manager(self, manager, *, settings=None):
        settings = settings or {
            "youtube_playlist_id": "current-default-must-not-win",
            "youtube_uploaded_files": [],
        }
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(dashboard, "JOB_MANAGER", manager)
            )
            stack.enter_context(
                mock.patch.object(dashboard, "jobs", manager.jobs)
            )
            stack.enter_context(
                mock.patch.object(dashboard, "job_lock", manager.lock)
            )
            stack.enter_context(
                mock.patch.object(dashboard, "job_counter", manager.counter)
            )
            stack.enter_context(
                mock.patch.object(dashboard, "MEDIA_ROOT", self.media)
            )
            stack.enter_context(
                mock.patch.object(
                    dashboard, "load_settings", return_value=settings
                )
            )
            stack.enter_context(
                mock.patch.object(
                    dashboard,
                    "local_video_metadata_payload",
                    return_value={"already_uploaded": False},
                )
            )
            yield dashboard.app.test_client()

    @staticmethod
    def csrf(client):
        return client.get("/api/auth/status").get_json()["csrf_token"]

    def retry_request(self, client, job_id, item_id):
        return client.post(
            "/api/jobs/retry-item",
            json={"job_id": job_id, "item_id": item_id},
            headers={"X-CSRF-Token": self.csrf(client)},
        )

    def test_interrupted_download_reasons_create_fresh_linked_jobs(self):
        archive = self.root / "archive.txt"
        partial = self.media / "existing.mp4.part"
        archive.write_text("1234567890\n", encoding="utf-8")
        partial.write_bytes(b"partial")
        original_files = (archive.read_bytes(), partial.read_bytes())

        for active, reason in (
            (False, "restart_before_start"),
            (True, "restart_interrupted"),
        ):
            with self.subTest(reason=reason):
                manager, store, job_id = self.restored_download(
                    f"download-{reason}.json", active=active
                )
                item_id = manager.get_job(job_id)["item_ids"][0]
                observed = {}

                def worker_start(_target, retry_job_id):
                    durable = JobStore(store.path).load().jobs
                    parent = next(job for job in durable if job["id"] == job_id)
                    retry_job = next(
                        job for job in durable if job["id"] == retry_job_id
                    )
                    observed["parent"] = parent
                    observed["retry"] = retry_job

                with self.app_manager(manager) as client, mock.patch.object(
                    manager, "start_worker", side_effect=worker_start
                ) as starter:
                    response = self.retry_request(client, job_id, item_id)
                    duplicate = self.retry_request(client, job_id, item_id)

                self.assertEqual(response.status_code, 200)
                retry_id = response.get_json()["retry_job_id"]
                self.assertNotEqual(retry_id, job_id)
                original = manager.get_job(job_id)
                retry_job = manager.get_job(retry_id)
                self.assertEqual(original["item_states"], ["interrupted"])
                self.assertEqual(
                    original["item_recovery_reasons"], [reason]
                )
                self.assertEqual(original["item_retry_job_ids"], [retry_id])
                self.assertEqual(
                    retry_job["urls"],
                    ["https://www.twitch.tv/videos/1234567890"],
                )
                self.assertEqual(
                    retry_job["retry_of"],
                    {"job_id": job_id, "item_id": item_id},
                )
                self.assertEqual(
                    observed["parent"]["item_retry_job_ids"], [retry_id]
                )
                self.assertEqual(
                    observed["retry"]["retry_of"]["item_id"], item_id
                )
                starter.assert_called_once()
                self.assertTrue(duplicate.get_json()["duplicate"])
                self.assertEqual(
                    duplicate.get_json()["retry_job_id"], retry_id
                )

        self.assertEqual(archive.read_bytes(), original_files[0])
        self.assertEqual(partial.read_bytes(), original_files[1])

    def test_noncanonical_download_is_blocked_without_worker(self):
        manager, _, job_id = self.restored_download("unsafe-download.json")
        item_id = manager.get_job(job_id)["item_ids"][0]
        manager.jobs[job_id]["urls"][0] = (
            "https://www.twitch.tv/videos/1234567890?token=secret"
        )

        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter:
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "unsafe_source_path")
        self.assertNotIn("secret", str(response.get_json()).lower())
        self.assertEqual(manager.get_job(job_id)["item_retry_job_ids"], [""])
        starter.assert_not_called()

    def test_mixed_download_batch_retries_only_interrupted_item(self):
        urls = [
            "https://www.twitch.tv/videos/1234567890",
            "https://www.twitch.tv/videos/2345678901",
            "https://www.twitch.tv/videos/3456789012",
        ]
        store = self.store("batch.json")
        source = self.manager(store)
        job_id = source.create_download_job(urls, "Batch")
        source.update_job(
            job_id,
            state="running",
            item_states=["completed", "running", "completed"],
            item_statuses=["fertig", "läuft", "fertig"],
        )
        manager = self.manager(store, restarted=True)
        manager.restore_from_store()
        snapshot = manager.snapshot_jobs()[0]
        capabilities = snapshot["item_capabilities"]
        self.assertEqual(
            [value["can_retry"] for value in capabilities],
            [False, True, False],
        )

        item_id = snapshot["item_ids"][1]
        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ):
            response = self.retry_request(client, job_id, item_id)

        retry = manager.get_job(response.get_json()["retry_job_id"])
        self.assertEqual(retry["urls"], [urls[1]])
        self.assertEqual(manager.get_job(job_id)["item_states"], [
            "completed",
            "interrupted",
            "completed",
        ])

    def test_safe_upload_retry_revalidates_and_freezes_metadata(self):
        manager, store, job_id, path = self.restored_upload(
            "safe-upload.json"
        )
        item_id = manager.get_job(job_id)["item_ids"][0]
        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter, mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=AssertionError("YouTube must stay offline"),
        ):
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 200)
        retry_id = response.get_json()["retry_job_id"]
        retry = manager.get_job(retry_id)
        original = manager.get_job(job_id)
        self.assertEqual(retry["playlist_id"], "playlist-job")
        self.assertEqual(
            retry["item_metadata"][0]["youtube_playlist_id"],
            "playlist-item",
        )
        self.assertEqual(
            retry["item_metadata"][0]["title"], "Frozen Twitch title"
        )
        self.assertEqual(retry["item_metadata"][0]["streamer"], "nika_livetv")
        self.assertEqual(retry["item_metadata"][0]["vod_id"], "1234567890")
        self.assertEqual(retry["item_metadata"][0]["size_bytes"], path.stat().st_size)
        self.assertEqual(original["item_states"], ["interrupted"])
        self.assertEqual(original["item_retry_job_ids"], [retry_id])
        durable = JobStore(store.path).load().jobs
        self.assertTrue(any(job.get("retry_of") for job in durable))
        starter.assert_called_once()

    def test_upload_missing_changed_and_partial_sources_are_blocked(self):
        cases = ("missing", "changed", "partial")
        for case in cases:
            with self.subTest(case=case):
                manager, _, job_id, path = self.restored_upload(
                    f"upload-{case}.json"
                )
                item_id = manager.get_job(job_id)["item_ids"][0]
                if case == "missing":
                    path.unlink()
                    expected = "source_missing"
                elif case == "changed":
                    path.write_bytes(b"different-size-video")
                    expected = "source_changed"
                else:
                    partial = self.media / "unsafe.mp4.part"
                    partial.write_bytes(b"partial")
                    manager.jobs[job_id]["urls"][0] = "unsafe.mp4.part"
                    expected = "unsafe_source_path"

                with self.app_manager(manager) as client, mock.patch.object(
                    manager, "start_worker"
                ) as starter:
                    response = self.retry_request(client, job_id, item_id)

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["reason"], expected)
                self.assertEqual(
                    manager.get_job(job_id)["item_retry_job_ids"], [""]
                )
                starter.assert_not_called()

    def test_symlink_escape_upload_source_is_blocked(self):
        manager, _, job_id, path = self.restored_upload("symlink.json")
        item_id = manager.get_job(job_id)["item_ids"][0]
        outside = self.root / "outside.mp4"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter:
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "unsafe_source_path")
        starter.assert_not_called()

    def test_outside_root_upload_source_is_blocked(self):
        manager, _, job_id, _ = self.restored_upload("outside.json")
        item_id = manager.get_job(job_id)["item_ids"][0]
        outside = self.root / "outside-direct.mp4"
        outside.write_bytes(b"original-video")
        manager.jobs[job_id]["urls"][0] = str(outside)

        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter:
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "unsafe_source_path")
        self.assertNotIn(str(outside), str(response.get_json()))
        starter.assert_not_called()

    def test_mixed_upload_batch_capabilities_are_item_specific(self):
        paths = []
        metadata = []
        for index in range(3):
            path = self.media / f"batch-{index}.mp4"
            path.write_bytes(b"video")
            paths.append(str(path))
            metadata.append(self.upload_metadata(path))
        store = self.store("upload-batch.json")
        source = self.manager(store)
        job_id = source.create_upload_job(
            paths, "Upload batch", item_metadata=metadata
        )
        source.update_job(
            job_id,
            state="running",
            item_states=["completed", "queued", "running"],
            item_statuses=["fertig", "wartet", "läuft"],
        )
        manager = self.manager(store, restarted=True)
        manager.restore_from_store()

        job = manager.snapshot_jobs()[0]
        self.assertEqual(
            job["item_states"],
            ["completed", "interrupted", "interrupted"],
        )
        self.assertEqual(
            [item["can_retry"] for item in job["item_capabilities"]],
            [False, True, False],
        )
        self.assertEqual(
            job["item_capabilities"][2]["retry_blocked_reason"],
            "review_required",
        )

    def test_uncertain_upload_is_review_required_without_api_or_worker(self):
        manager, _, job_id, _ = self.restored_upload(
            "uncertain-upload.json", active=True
        )
        job = manager.get_job(job_id)
        item_id = job["item_ids"][0]
        capability = manager.snapshot_jobs()[0]["item_capabilities"][0]
        self.assertFalse(capability["can_retry"])
        self.assertEqual(
            capability["retry_blocked_reason"], "review_required"
        )

        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter, mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=AssertionError("YouTube must stay offline"),
        ):
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "review_required")
        self.assertTrue(response.get_json()["outcome_uncertain"])
        starter.assert_not_called()

    def test_known_upload_failure_remains_retryable_but_uncertain_does_not(self):
        for failure_kind, expected in (("known", True), ("uncertain", False)):
            manager = JobManager()
            job_id = manager.create_upload_job(["vod.mp4"], "Upload")
            item_id = manager.get_job(job_id)["item_ids"][0]
            manager.finish_claimed_item(
                job_id,
                item_id,
                "failed",
                failure_kind=failure_kind,
            )
            capability = manager.snapshot_jobs()[0]["item_capabilities"][0]
            self.assertEqual(capability["can_retry"], expected)
            if not expected:
                self.assertEqual(
                    capability["retry_blocked_reason"], "review_required"
                )

    def test_recording_retry_is_blocked_without_p4_or_process_actions(self):
        for state in ("queued", "running", "failed"):
            with self.subTest(state=state):
                store = self.store(f"recording-{state}.json")
                source = self.manager(store)
                job_id = source.create_recording_job(
                    "nika_livetv",
                    output_name="nika_livetv/live.%(ext)s",
                )
                if state != "queued":
                    claim = source.claim_recording_job(job_id)
                    if state == "failed":
                        source.finalize_recording_job(
                            job_id,
                            claim["item_id"],
                            state="failed",
                            returncode=1,
                            completion_reason="process_error",
                        )
                manager = self.manager(store, restarted=True)
                manager.restore_from_store()
                item_id = manager.get_job(job_id)["item_ids"][0]
                capability = manager.snapshot_jobs()[0][
                    "item_capabilities"
                ][0]
                self.assertFalse(capability["can_retry"])
                self.assertEqual(
                    capability["retry_blocked_reason"],
                    "recording_retry_unsupported",
                )

                with self.app_manager(manager) as client, mock.patch.object(
                    manager, "start_worker"
                ) as starter, mock.patch.object(
                    dashboard, "run_ytdlp_live_status"
                ) as live_status, mock.patch(
                    "vod_dashboard.auto_recorder.AutoRecorderStateStore._write_locked"
                ) as auto_write:
                    response = self.retry_request(client, job_id, item_id)

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.get_json()["reason"],
                    "recording_retry_unsupported",
                )
                starter.assert_not_called()
                live_status.assert_not_called()
                auto_write.assert_not_called()

    def test_required_retry_save_failure_starts_nothing_and_consumes_id(self):
        store = self.store("failing.json", toggle=True)
        source = self.manager(store)
        job_id = source.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Download"
        )
        manager = self.manager(store, restarted=True)
        manager.restore_from_store()
        item_id = manager.get_job(job_id)["item_ids"][0]
        store.fail = True

        with self.app_manager(manager) as client, mock.patch.object(
            manager, "start_worker"
        ) as starter:
            response = self.retry_request(client, job_id, item_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["reason"], "persistence_unavailable"
        )
        starter.assert_not_called()
        self.assertFalse(manager.persistence_status()["healthy"])
        self.assertEqual(manager.counter, 2)
        failed_retry = manager.get_job("2")
        self.assertEqual(failed_retry["state"], "failed")
        self.assertEqual(
            failed_retry["recovery_reason"], "persistence_unavailable"
        )

        store.fail = False
        next_id = manager.create_download_job(
            ["https://www.twitch.tv/videos/2345678901"], "Next"
        )
        self.assertEqual(next_id, "3")

    def test_retry_of_retry_uses_existing_flat_relationship_model(self):
        manager = JobManager()
        first = manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "First"
        )
        first_item = manager.get_job(first)["item_ids"][0]
        manager.finish_claimed_item(first, first_item, "failed")
        manager.reserve_retry(first, first_item)
        second = manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"],
            "Second",
            retry_of={"job_id": first, "item_id": first_item},
        )
        second_item = manager.get_job(second)["item_ids"][0]
        manager.finish_claimed_item(second, second_item, "failed")
        manager.reserve_retry(second, second_item)
        third = manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"],
            "Third",
            retry_of={"job_id": second, "item_id": second_item},
        )

        self.assertEqual(manager.get_job(first)["item_retry_job_ids"], [second])
        self.assertEqual(manager.get_job(second)["item_retry_job_ids"], [third])
        self.assertEqual(
            manager.get_job(third)["retry_of"],
            {"job_id": second, "item_id": second_item},
        )

    def test_cancelled_item_retry_behavior_is_not_broadened(self):
        manager = JobManager()
        job_id = manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Cancelled"
        )
        item_id = manager.get_job(job_id)["item_ids"][0]
        manager.remove_queued_item(job_id, item_id)

        capability = manager.snapshot_jobs()[0]["item_capabilities"][0]
        self.assertFalse(capability["can_retry"])
        self.assertIsNone(manager.reserve_retry(job_id, item_id))


def tearDownModule():
    if _IMPORT_TMP is not None:
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _IMPORT_TMP.cleanup()


if __name__ == "__main__":
    unittest.main()
