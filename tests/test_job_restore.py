import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard.job_store import JobStore, JobStorePersistenceError
from vod_dashboard.jobs import JobManager, JobRestoreError, JobRestoreResult


CREATED = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
RESTARTED = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)
RESTARTED_TEXT = "2026-08-24T12:30:00Z"


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


class SaveFailingStore(JobStore):
    def save(self, *args, **kwargs):
        raise JobStorePersistenceError("private disk detail")


class LoadExplodingStore:
    def load(self):
        raise RuntimeError("private load detail")


class JobRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, name="jobs.json", *, failing=False):
        store_type = SaveFailingStore if failing else JobStore
        return store_type(self.root / name, clock=lambda: CREATED)

    def manager(self, store=None, *, restarted=False):
        return JobManager(
            job_store=store,
            media_root=self.media,
            now=lambda: RESTARTED if restarted else CREATED,
        )

    def download(self, manager, urls=None):
        return manager.create_download_job(
            urls
            or ["https://www.twitch.tv/videos/1234567890"],
            "Download",
        )

    def upload(self, manager, names=None):
        names = names or ["vod.mp4"]
        paths = []
        metadata = []
        for name in names:
            path = self.media / name
            path.touch(exist_ok=True)
            paths.append(str(path))
            metadata.append(upload_metadata(name))
        return manager.create_upload_job(
            paths, "Upload", item_metadata=metadata
        )

    def recording(self, manager):
        return manager.create_recording_job(
            "nika_livetv",
            stream_id="987654321",
            live_started_at="2026-08-23T09:00:00Z",
            output_name="nika_livetv/live.%(ext)s",
            origin="auto",
            attempt=2,
        )

    def restore(self, name="jobs.json", *, store=None):
        target_store = store or JobStore(
            self.root / name, clock=lambda: RESTARTED
        )
        manager = self.manager(target_store, restarted=True)
        result = manager.restore_from_store()
        return manager, result, target_store

    def test_storeless_missing_and_failed_load_have_safe_results(self):
        storeless = self.manager(restarted=True)
        result = storeless.restore_from_store()
        self.assertEqual(
            result,
            JobRestoreResult(
                enabled=False,
                loaded_count=0,
                discarded_count=0,
                reconciled_job_count=0,
                reconciled_item_count=0,
                degraded=False,
                source="disabled",
                reason="store_disabled",
            ),
        )
        self.assertIs(storeless.restore_from_store(), result)

        empty, empty_result, _ = self.restore("missing.json")
        self.assertEqual(empty.jobs, {})
        self.assertFalse(empty_result.degraded)
        self.assertEqual(empty_result.reason, "missing")

        failed = self.manager(LoadExplodingStore(), restarted=True)
        failed_result = failed.restore_from_store()
        self.assertTrue(failed_result.degraded)
        self.assertEqual(failed_result.reason, "load_failed")
        self.assertNotIn("private", repr(failed_result))

    def test_restore_is_guarded_for_a_nonempty_manager(self):
        manager = self.manager()
        self.download(manager)
        with self.assertRaises(JobRestoreError) as raised:
            manager.restore_from_store()
        self.assertEqual(raised.exception.code, "manager_not_empty")

    def test_terminal_download_history_and_derived_shape_are_preserved(self):
        source_store = self.store()
        source = self.manager(source_store)
        terminal = {}
        for state in ("completed", "failed", "cancelled", "interrupted"):
            job_id = self.download(source)
            item_id = f"{job_id}-item-1"
            if state == "cancelled":
                source.remove_queued_item(job_id, item_id)
            else:
                source.claim_next_item(job_id)
                source.finish_claimed_item(
                    job_id,
                    item_id,
                    state,
                    failure_kind="known" if state == "failed" else "",
                )
            terminal[state] = source.get_job(job_id)

        restored, result, _ = self.restore()
        self.assertEqual(result.reconciled_item_count, 0)
        for state, original in terminal.items():
            job = restored.get_job(original["id"])
            self.assertEqual(job["type"], "download")
            self.assertEqual(job["state"], state)
            self.assertEqual(job["item_states"], [state])
            self.assertEqual(job["finished_at"], original["finished_at"])
            self.assertEqual(job["created"], "2026-08-23 10:00:00")
            self.assertEqual(job["status"], job["item_statuses"][0])
            self.assertEqual(job["log"], [])

    def test_high_water_and_compatibility_counter_never_reuse_ids(self):
        source_store = self.store()
        source = self.manager(source_store)
        for wanted in (1, 7, 12):
            source.counter = wanted - 1
            job_id = self.download(source)
            source.remove_queued_item(job_id, f"{job_id}-item-1")
        source_store.save(
            list(source.jobs.values()), 13, revision=50, media_root=self.media
        )

        restored, _, _ = self.restore(store=source_store)
        self.assertEqual(self.download(restored), "13")

        counter = {"value": 0}
        self.assertEqual(
            restored.create_download_job(
                ["https://www.twitch.tv/videos/2345678901"],
                "Compatibility",
                counter_getter=lambda: counter["value"],
                counter_setter=lambda value: counter.update(value=value),
            ),
            "14",
        )
        self.assertEqual(counter["value"], 14)

    def test_stored_high_water_above_max_id_is_preserved(self):
        source_store = self.store()
        source = self.manager(source_store)
        job_id = self.download(source)
        source.remove_queued_item(job_id, f"{job_id}-item-1")
        source_store.save(
            list(source.jobs.values()), 50, revision=50, media_root=self.media
        )

        restored, _, _ = self.restore(store=source_store)
        self.assertEqual(self.download(restored), "50")

    def _restore_download_state(self, state):
        name = f"download-{state}.json"
        source_store = self.store(name)
        source = self.manager(source_store)
        job_id = self.download(source)
        item_id = f"{job_id}-item-1"
        if state != "queued":
            source.claim_next_item(job_id)
            source.update_job(
                job_id,
                item_progress=[37.5],
                item_processed_seconds=[900.0],
                item_speed_multiplier=[41.0],
                item_speed_label=["41x"],
                item_eta_seconds=[30],
                item_updated_at=[123.0],
            )
        if state == "cancelling":
            source.request_cancel_item(job_id, item_id)
        return self.restore(name)

    def test_download_restart_matrix_preserves_progress_and_media(self):
        archive = self.root / "archive.txt"
        partial = self.media / "vod.mp4.part"
        archive.write_text("1234567890\n", encoding="utf-8")
        partial.write_bytes(b"partial-data")
        before_archive = archive.read_bytes()
        before_partial = partial.read_bytes()

        for state, reason in (
            ("queued", "restart_before_start"),
            ("running", "restart_interrupted"),
            ("cancelling", "restart_interrupted"),
        ):
            with self.subTest(state=state):
                restored, result, _ = self._restore_download_state(state)
                job = restored.get_job("1")
                self.assertEqual(job["state"], "interrupted")
                self.assertEqual(job["item_completion_reasons"], [reason])
                self.assertEqual(job["item_recovery_reasons"], [reason])
                self.assertIsNone(job["returncode"])
                if state != "queued":
                    self.assertEqual(job["item_progress"], [37.5])
                    self.assertEqual(job["item_processed_seconds"], [900.0])
                self.assertEqual(job["item_speed_multiplier"], [None])
                self.assertEqual(job["item_speed_label"], [""])
                self.assertEqual(job["item_eta_seconds"], [None])
                self.assertEqual(job["item_updated_at"], [None])
                self.assertEqual(result.reconciled_item_count, 1)

        self.assertEqual(archive.read_bytes(), before_archive)
        self.assertEqual(partial.read_bytes(), before_partial)

    def _restore_upload_state(self, state):
        name = f"upload-{state}.json"
        source_store = self.store(name)
        source = self.manager(source_store)
        job_id = self.upload(source)
        item_id = f"{job_id}-item-1"
        if state != "queued":
            source.claim_next_item(job_id)
            source.update_job(
                job_id,
                item_progress=[42.0],
                item_bytes_uploaded=[42],
                item_total_bytes=[100],
                item_bytes_per_second=[50.0],
                item_eta_seconds=[2],
                item_updated_at=[123.0],
            )
        if state == "cancelling":
            source.request_cancel_item(job_id, item_id)
        return self.restore(name)

    def test_upload_restart_matrix_is_conservative_and_offline(self):
        for state, reason, failure_kind in (
            ("queued", "restart_before_start", ""),
            ("running", "upload_status_unknown", "uncertain"),
            ("cancelling", "upload_status_unknown", "uncertain"),
        ):
            with self.subTest(state=state):
                with mock.patch(
                    "vod_dashboard.youtube.get_youtube_service",
                    side_effect=AssertionError("YouTube must stay offline"),
                ):
                    restored, _, _ = self._restore_upload_state(state)
                job = restored.get_job("1")
                self.assertEqual(job["state"], "interrupted")
                self.assertEqual(job["item_recovery_reasons"], [reason])
                self.assertEqual(job["item_failure_kinds"], [failure_kind])
                self.assertEqual(job["item_bytes_per_second"], [None])
                self.assertEqual(job["item_eta_seconds"], [None])
                self.assertEqual(job["item_updated_at"], [None])
                self.assertEqual(job["item_errors"], [""])
                if state != "queued":
                    self.assertEqual(job["item_bytes_uploaded"], [42])

    def _restore_recording_state(self, state):
        name = f"recording-{state}.json"
        source_store = self.store(name)
        source = self.manager(source_store)
        job_id = self.recording(source)
        if state != "queued":
            source.claim_recording_job(job_id)
            source.update_recorded_seconds(job_id, 123.5)
        if state == "stopping":
            source.request_recording_stop(job_id)
        return self.restore(name)

    def test_recording_restart_matrix_preserves_intent_without_processes(self):
        for state, reason in (
            ("queued", "restart_before_start"),
            ("running", "restart_interrupted"),
            ("stopping", "restart_interrupted"),
        ):
            with self.subTest(state=state):
                restored, _, _ = self._restore_recording_state(state)
                job = restored.get_job("1")
                self.assertEqual(job["state"], "interrupted")
                self.assertEqual(job["item_recovery_reasons"], [reason])
                self.assertFalse(job["output_complete"])
                self.assertEqual(job["recorded_seconds"], 123.5 if state != "queued" else 0.0)
                self.assertEqual(job["stop_requested"], state == "stopping")
                self.assertIsNone(restored.recording_process("1"))
                self.assertFalse(restored.is_recording_active())

    def test_terminal_recording_outcomes_remain_unchanged(self):
        for sequence, state, reason in (
            (1, "completed", "natural_end"),
            (2, "completed", "stopped_by_user"),
            (3, "failed", "process_error"),
        ):
            name = f"terminal-recording-{sequence}.json"
            source = self.manager(self.store(name))
            job_id = self.recording(source)
            claim = source.claim_recording_job(job_id)
            output = self.media / f"recording-{sequence}.mp4"
            output.touch()
            source.finalize_recording_job(
                job_id,
                claim["item_id"],
                state=state,
                returncode=0 if state == "completed" else 1,
                completion_reason=reason,
                output_path=str(output) if state == "completed" else None,
            )
            original = source.get_job(job_id)

            restored, result, _ = self.restore(name)
            job = restored.get_job(job_id)
            self.assertEqual(job["state"], state)
            self.assertEqual(job["completion_reason"], reason)
            self.assertEqual(job["output_complete"], state == "completed")
            self.assertEqual(job["finished_at"], original["finished_at"])
            self.assertEqual(result.reconciled_item_count, 0)

    def test_terminal_upload_outcomes_remain_unchanged(self):
        for sequence, state in enumerate(("completed", "failed"), start=1):
            name = f"terminal-upload-{sequence}.json"
            source = self.manager(self.store(name))
            job_id = self.upload(source, [f"terminal-{sequence}.mp4"])
            claim = source.claim_next_item(job_id)
            source.finish_claimed_item(
                job_id,
                claim["item_id"],
                state,
                failure_kind="known" if state == "failed" else "",
            )
            original = source.get_job(job_id)

            restored, result, _ = self.restore(name)
            job = restored.get_job(job_id)
            self.assertEqual(job["state"], state)
            self.assertEqual(job["item_failure_kinds"], original["item_failure_kinds"])
            self.assertEqual(job["finished_at"], original["finished_at"])
            self.assertEqual(result.reconciled_item_count, 0)

    def test_valid_retry_links_restore_but_pending_sentinels_do_not(self):
        source = self.manager(self.store())
        parent = self.download(source)
        claim = source.claim_next_item(parent)
        source.finish_claimed_item(
            parent, claim["item_id"], "failed", failure_kind="known"
        )
        child = self.download(source)
        source.update_job(
            child,
            retry_of={"job_id": parent, "item_id": claim["item_id"]},
        )
        source.finalize_retry(parent, claim["item_id"], child)
        source.remove_queued_item(child, f"{child}-item-1")
        pending_parent = self.download(source)
        pending_claim = source.claim_next_item(pending_parent)
        source.finish_claimed_item(
            pending_parent,
            pending_claim["item_id"],
            "failed",
            failure_kind="known",
        )
        self.assertTrue(
            source.reserve_retry(
                pending_parent, pending_claim["item_id"]
            )["reserved"]
        )
        source.update_job(pending_parent, label="Pending retry")

        restored, _, _ = self.restore()
        self.assertEqual(
            restored.get_job(child)["retry_of"],
            {"job_id": parent, "item_id": claim["item_id"]},
        )
        self.assertEqual(
            restored.get_job(parent)["item_retry_job_ids"], [child]
        )
        self.assertNotIn(
            "__pending__",
            restored.get_job(parent)["item_retry_job_ids"],
        )
        self.assertEqual(
            restored.get_job(pending_parent)["item_retry_job_ids"], [""]
        )

    def test_mixed_batches_aggregate_to_interrupted(self):
        download_store = self.store("batch-download.json")
        source = self.manager(download_store)
        job_id = self.download(
            source,
            [
                "https://www.twitch.tv/videos/1234567890",
                "https://www.twitch.tv/videos/2345678901",
            ],
        )
        first = source.claim_next_item(job_id)
        source.finish_claimed_item(job_id, first["item_id"], "completed")
        second = source.claim_next_item(job_id)
        original_started = source.get_job(job_id)["started_at"]
        restored, _, _ = self.restore("batch-download.json")
        job = restored.get_job(job_id)
        self.assertEqual(job["item_states"], ["completed", "interrupted"])
        self.assertEqual(
            job["item_recovery_reasons"], ["", "restart_interrupted"]
        )
        self.assertEqual(job["state"], "interrupted")
        self.assertEqual(job["started_at"], original_started)
        self.assertEqual(job["finished_at"], RESTARTED_TEXT)
        self.assertEqual(second["item_id"], "1-item-2")

        upload_source = self.manager(self.store("batch-upload.json"))
        upload_id = self.upload(upload_source, ["one.mp4", "two.mp4"])
        first = upload_source.claim_next_item(upload_id)
        upload_source.finish_claimed_item(
            upload_id, first["item_id"], "completed"
        )
        restored_upload, _, _ = self.restore("batch-upload.json")
        upload_job = restored_upload.get_job(upload_id)
        self.assertEqual(
            upload_job["item_states"], ["completed", "interrupted"]
        )
        self.assertEqual(
            upload_job["item_recovery_reasons"], ["", "restart_before_start"]
        )
        self.assertEqual(upload_job["state"], "interrupted")

    def test_reconciliation_timestamps_are_durable_and_idempotent(self):
        source_store = self.store()
        source = self.manager(source_store)
        job_id = self.download(source)
        source.claim_next_item(job_id)
        original = source.get_job(job_id)
        before_revision = source_store.status()["last_written_revision"]

        restored, result, _ = self.restore(store=source_store)
        job = restored.get_job(job_id)
        self.assertEqual(job["created_at"], original["created_at"])
        self.assertEqual(job["started_at"], original["started_at"])
        self.assertEqual(job["updated_at"], RESTARTED_TEXT)
        self.assertEqual(job["finished_at"], RESTARTED_TEXT)
        self.assertGreater(
            source_store.status()["last_written_revision"], before_revision
        )
        self.assertIs(restored.restore_from_store(), result)
        self.assertEqual(restored.get_job(job_id)["finished_at"], RESTARTED_TEXT)

        second, second_result, _ = self.restore()
        self.assertEqual(second_result.reconciled_item_count, 0)
        self.assertEqual(second.get_job(job_id)["finished_at"], RESTARTED_TEXT)

    def test_reconciliation_save_failure_keeps_truthful_memory(self):
        source = self.manager(self.store())
        job_id = self.download(source)
        source.claim_next_item(job_id)
        failing = SaveFailingStore(
            self.root / "jobs.json", clock=lambda: RESTARTED
        )

        restored, result, _ = self.restore(store=failing)
        self.assertEqual(restored.get_job(job_id)["state"], "interrupted")
        self.assertTrue(result.degraded)
        status = restored.persistence_status()
        self.assertFalse(status["healthy"])
        self.assertEqual(
            status["last_error_code"], "persistence_unavailable"
        )

    def test_corrupt_primary_is_not_overwritten(self):
        path = self.root / "corrupt.json"
        path.write_bytes(b"{not-json")
        before = path.read_bytes()

        restored, result, _ = self.restore("corrupt.json")
        self.assertEqual(restored.jobs, {})
        self.assertTrue(result.degraded)
        self.assertEqual(result.reason, "invalid_json")
        self.assertEqual(path.read_bytes(), before)

    def test_degraded_recovered_jobs_and_unknown_fields_are_safe(self):
        source = self.manager(self.store())
        first = self.download(source)
        source.remove_queued_item(first, f"{first}-item-1")
        path = self.root / "jobs.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["jobs"][0]["raw_secret"] = "must-not-restore"
        value["jobs"][0]["log"] = ["token=secret"]
        value["jobs"].append({"id": "broken"})
        path.write_text(json.dumps(value), encoding="utf-8")

        restored, result, _ = self.restore()
        job = restored.get_job(first)
        self.assertEqual(result.loaded_count, 1)
        self.assertEqual(result.discarded_count, 1)
        self.assertTrue(result.degraded)
        self.assertNotIn("raw_secret", job)
        self.assertEqual(job["log"], [])
        status = restored.persistence_status()
        self.assertTrue(status["load_degraded"])
        self.assertEqual(status["load_source"], "primary")

    def test_restore_has_no_runtime_ownership_or_external_side_effects(self):
        source = self.manager(self.store())
        job_id = self.download(source)
        source.claim_next_item(job_id)
        media = self.media / "existing.part"
        media.write_bytes(b"unchanged")
        before = media.read_bytes()

        with mock.patch(
            "vod_dashboard.jobs.threading.Thread",
            side_effect=AssertionError("worker must not start"),
        ), mock.patch(
            "vod_dashboard.jobs.subprocess.Popen",
            side_effect=AssertionError("process must not start"),
        ), mock.patch(
            "vod_dashboard.auto_recorder.AutoRecorderStateStore._write_locked",
            side_effect=AssertionError("auto recorder store must not change"),
        ):
            restored, _, _ = self.restore()

        controls = restored.queue_controls_snapshot()
        self.assertFalse(controls["download"]["has_active_item"])
        self.assertFalse(controls["download"]["queue_paused"])
        self.assertFalse(controls["download"]["stop_after_current"])
        self.assertEqual(restored._cancel_events, {})
        self.assertEqual(restored._download_processes, {})
        self.assertEqual(restored._recording_processes, {})
        self.assertEqual(restored._recording_stop_events, {})
        self.assertEqual(media.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
