import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard import job_store


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.temp_dir.name) / "data"
        self.path = self.dashboard_dir / "jobs.json"
        self.now = datetime(2026, 8, 23, 18, 10, tzinfo=timezone.utc)
        self.store = job_store.JobStore.from_dashboard_dir(
            self.dashboard_dir, clock=lambda: self.now
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def common(job_id="1", job_type="download", state="queued", **updates):
        payload = {
            "id": str(job_id),
            "type": job_type,
            "label": f"Job {job_id}",
            "created_at": "2026-08-23T18:00:00Z",
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "state": state,
            "completion_reason": "",
            "returncode": None,
            "item_ids": [f"{job_id}-item-1"],
            "item_states": [state],
            "item_completion_reasons": [""],
            "item_failure_kinds": [""],
            "item_resolved": [False],
            "item_retry_job_ids": [""],
        }
        payload.update(updates)
        return payload

    @classmethod
    def download(cls, job_id="1", state="queued", **updates):
        payload = cls.common(job_id, "download", state)
        payload.update(
            {
                "urls": [f"https://www.twitch.tv/videos/{1234560000 + int(job_id)}"],
                "total_urls": 1,
                "item_progress": [None],
                "item_processed_seconds": [None],
                "item_total_duration_seconds": [None],
                "item_updated_at": [None],
            }
        )
        payload.update(updates)
        return payload

    @classmethod
    def upload(cls, job_id="2", state="queued", **updates):
        payload = cls.common(job_id, "youtube_upload", state)
        payload.update(
            {
                "urls": ["streamer/video.mp4"],
                "playlist_id": "PL_GLOBAL",
                "item_metadata": [
                    {
                        "streamer": "nika_livetv",
                        "date": "23.08.2026",
                        "title": "Safe Twitch title",
                        "vod_id": "1234567890",
                        "name": "video.mp4",
                        "size_bytes": 123456,
                        "size_gb": 0.001,
                        "youtube_playlist_id": "PL_STREAMER",
                    }
                ],
                "item_progress": [None],
                "item_bytes_uploaded": [None],
                "item_total_bytes": [123456],
                "item_updated_at": [None],
            }
        )
        payload.update(updates)
        return payload

    @classmethod
    def recording(cls, job_id="3", state="running", **updates):
        payload = cls.common(job_id, "recording", state)
        payload.update(
            {
                "streamer": "nika_livetv",
                "stream_id": "LIVE_123",
                "origin": "auto",
                "attempt": 2,
                "title": "Live title",
                "live_started_at": "2026-08-23T19:00:00+01:00",
                "quality": "source/best",
                "output_name": "nika_livetv/live-%(id)s.%(ext)s",
                "output_path": None,
                "output_complete": False,
                "recorded_seconds": 42.5,
                "stop_requested": False,
            }
        )
        payload.update(updates)
        return payload

    def write_state(self, value):
        self.dashboard_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def persisted_state(self, jobs, next_job_id=10, saved_at="2026-08-23T18:10:00Z"):
        return {
            "version": 1,
            "next_job_id": next_job_id,
            "saved_at": saved_at,
            "jobs": jobs,
        }

    def test_path_factory_and_missing_file_are_healthy_empty_without_writing(self):
        result = self.store.load()

        self.assertEqual(job_store.job_store_path(self.dashboard_dir), self.path)
        self.assertEqual(self.store.path, self.path)
        self.assertEqual(result.state, job_store.empty_job_store_state())
        self.assertTrue(result.healthy)
        self.assertFalse(result.degraded)
        self.assertEqual(result.source, "empty")
        self.assertEqual(result.reason, "missing")
        self.assertFalse(self.path.exists())

    def test_valid_empty_store_round_trip(self):
        saved = self.store.save([], 1, revision=0)
        loaded = self.store.load()

        self.assertTrue(saved.saved)
        self.assertEqual(loaded.jobs, [])
        self.assertEqual(loaded.next_job_id, 1)
        self.assertEqual(loaded.state["saved_at"], "2026-08-23T18:10:00Z")
        self.assertTrue(loaded.healthy)

    def test_valid_mixed_job_types_round_trip_and_download_type_is_explicit(self):
        download = self.download("1")
        download.pop("type")
        jobs = [download, self.upload("2"), self.recording("3")]

        saved = self.store.save(jobs, 4, revision=1)
        loaded = self.store.load()

        self.assertEqual([value["type"] for value in loaded.jobs], [
            "download", "youtube_upload", "recording"
        ])
        self.assertEqual(saved.state, loaded.state)
        self.assertEqual(
            loaded.jobs[2]["live_started_at"], "2026-08-23T18:00:00Z"
        )

    def test_auto_vod_download_metadata_round_trips_and_is_strict(self):
        auto_vod = self.download(
            origin="auto_vod",
            streamer="Nika_LiveTV",
            twitch_vod_id="1234567890",
            attempt=2,
            post_download_mode="download_only",
        )
        self.store.save([auto_vod], 2, revision=1)
        loaded = self.store.load().jobs[0]

        self.assertEqual(
            {
                key: loaded[key]
                for key in (
                    "origin",
                    "streamer",
                    "twitch_vod_id",
                    "attempt",
                    "post_download_mode",
                )
            },
            {
                "origin": "auto_vod",
                "streamer": "nika_livetv",
                "twitch_vod_id": "1234567890",
                "attempt": 2,
                "post_download_mode": "download_only",
            },
        )
        with self.assertRaisesRegex(
            job_store.JobStoreValidationError,
            "invalid_auto_vod_download_metadata",
        ):
            job_store.serialize_job(
                self.download(origin="auto_vod", streamer="", twitch_vod_id="1234567890")
            )

    def test_timezone_aware_job_timestamps_are_normalized_and_naive_rejected(self):
        value = self.download(
            created_at="2026-08-23T20:00:00+02:00",
            started_at="2026-08-23T18:01:00Z",
        )
        self.assertEqual(
            job_store.serialize_job(value)["created_at"],
            "2026-08-23T18:00:00Z",
        )
        value["created_at"] = "2026-08-23T18:00:00"
        with self.assertRaisesRegex(job_store.JobStoreValidationError, "invalid_created_at"):
            job_store.serialize_job(value)

    def test_unsupported_version_invalid_json_and_wrong_top_level_are_degraded(self):
        cases = (
            (json.dumps({"version": 2, "next_job_id": 1, "saved_at": "2026-08-23T18:00:00Z", "jobs": []}), "unsupported_version"),
            ('{"secret":"PRESERVE", broken', "invalid_json"),
            (json.dumps(["wrong"]), "invalid_structure"),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                self.dashboard_dir.mkdir(parents=True, exist_ok=True)
                self.path.write_text(raw, encoding="utf-8")
                before = self.path.read_bytes()

                result = self.store.load()

                self.assertTrue(result.degraded)
                self.assertFalse(result.healthy)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.source, "empty")
                self.assertEqual(self.path.read_bytes(), before)

    def test_load_discards_malformed_unknown_and_duplicate_jobs_but_keeps_valid_sibling(self):
        valid = self.download("7")
        malformed = self.download("8", item_states=[])
        unknown = self.common("9", "future_job")
        duplicate = self.download("7")
        self.write_state(
            self.persisted_state([malformed, valid, unknown, duplicate], 20)
        )

        result = self.store.load()

        self.assertEqual([value["id"] for value in result.jobs], ["7"])
        self.assertEqual(result.discarded_job_count, 3)
        self.assertTrue(result.degraded)
        self.assertEqual(result.reason, "discarded_jobs")

    def test_nonterminal_states_load_without_restart_reconciliation(self):
        running = self.download("7", state="running")
        stopping = self.recording("8", state="stopping")
        self.write_state(self.persisted_state([running, stopping], 9))

        result = self.store.load()

        self.assertEqual(
            [value["state"] for value in result.jobs],
            ["running", "stopping"],
        )

    def test_unhashable_malformed_json_fields_discard_only_that_job(self):
        cases = (
            {"type": ["download"]},
            {"state": ["running"]},
            {"item_states": [["running"]]},
            {"item_failure_kinds": [["known"]]},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                malformed = self.download("6", **updates)
                self.write_state(
                    self.persisted_state([malformed, self.download("7")], 8)
                )
                result = self.store.load()
                self.assertEqual([value["id"] for value in result.jobs], ["7"])
                self.assertEqual(result.discarded_job_count, 1)

    def test_persisted_version_one_job_requires_explicit_type(self):
        implicit = self.download("6")
        implicit.pop("type")
        self.write_state(
            self.persisted_state([implicit, self.download("7")], 8)
        )

        result = self.store.load()

        self.assertEqual([value["id"] for value in result.jobs], ["7"])
        self.assertEqual(result.discarded_job_count, 1)

    def test_malformed_next_id_uses_bounded_high_fallback_and_reports_degraded(self):
        self.write_state(self.persisted_state([self.download("42")], "broken"))

        result = self.store.load()

        self.assertEqual(result.next_job_id, job_store.SAFE_NEXT_JOB_ID_FALLBACK)
        self.assertTrue(result.degraded)
        self.assertEqual(result.reason, "invalid_next_job_id")

    def test_next_id_is_never_below_maximum_job_id_plus_one(self):
        self.write_state(self.persisted_state([self.download("42")], 4))

        result = self.store.load()

        self.assertEqual(result.next_job_id, 43)
        self.assertTrue(result.degraded)

    def test_job_ids_are_positive_bounded_decimal_strings(self):
        for value in ("", "0", "-1", " 1", "1 ", "1.0", "uuid-value", "../1", "10000000000"):
            with self.subTest(value=value), self.assertRaisesRegex(
                job_store.JobStoreValidationError, "invalid_job_id"
            ):
                job_store.serialize_job(self.download("1", id=value))

    def test_save_corrects_low_but_valid_next_id(self):
        saved = self.store.save([self.download("42")], 4, revision=1)
        self.assertEqual(saved.state["next_job_id"], 43)

    def test_download_requires_exact_canonical_twitch_vod_url(self):
        accepted = job_store.serialize_job(self.download())
        self.assertEqual(
            accepted["urls"], ["https://www.twitch.tv/videos/1234560001"]
        )
        for url in (
            "http://www.twitch.tv/videos/1234560001",
            "https://twitch.tv/videos/1234560001",
            "https://www.twitch.tv/videos/1234560001?token=SECRET",
            "https://cloudfront.invalid/signed.m3u8",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                job_store.JobStoreValidationError, "invalid_twitch_vod_url"
            ):
                job_store.serialize_job(self.download(urls=[url]))

    def test_upload_relative_paths_are_posix_normalized(self):
        normalized = job_store.serialize_job(
            self.upload(urls=["streamer\\folder\\video.mp4"])
        )
        self.assertEqual(normalized["urls"], ["streamer/folder/video.mp4"])

    def test_upload_absolute_traversal_url_and_control_paths_are_rejected(self):
        values = (
            str((self.dashboard_dir / "video.mp4").resolve()),
            "C:\\media\\video.mp4",
            "../outside/video.mp4",
            "folder/../video.mp4",
            "https://signed.invalid/video.mp4",
            "folder/evil\x00.mp4",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaisesRegex(
                job_store.JobStoreValidationError, "invalid_media_path"
            ):
                job_store.serialize_job(self.upload(urls=[value]))

    def test_serializer_can_convert_contained_absolute_runtime_path_when_root_is_explicit(self):
        media_root = self.dashboard_dir / "media"
        video = media_root / "streamer" / "video.mp4"
        normalized = job_store.serialize_job(
            self.upload(urls=[str(video)]), media_root=media_root
        )
        self.assertEqual(normalized["urls"], ["streamer/video.mp4"])

    def test_historical_relative_media_path_does_not_need_to_exist(self):
        self.store.save([self.upload(urls=["deleted/video.mp4"])], 3, revision=1)
        self.assertEqual(self.store.load().jobs[0]["urls"], ["deleted/video.mp4"])

    def test_recording_relative_paths_origin_attempt_and_stream_id_are_safe(self):
        normalized = job_store.serialize_job(
            self.recording(
                output_path="nika_livetv/finished.mp4",
                output_complete=True,
            )
        )
        self.assertEqual(normalized["origin"], "auto")
        self.assertEqual(normalized["attempt"], 2)
        self.assertEqual(normalized["output_path"], "nika_livetv/finished.mp4")
        self.assertTrue(normalized["output_complete"])

    def test_recording_rejects_malformed_stream_id_output_and_metadata(self):
        cases = (
            ({"stream_id": "https://signed.invalid/live"}, "invalid_recording_stream_id"),
            ({"output_name": "../outside.mp4"}, "invalid_output_name"),
            ({"origin": "future"}, "invalid_recording_origin"),
            ({"attempt": 0}, "invalid_recording_attempt"),
        )
        for updates, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                job_store.JobStoreValidationError, reason
            ):
                job_store.serialize_job(self.recording(**updates))

    def test_misaligned_item_arrays_reject_whole_job(self):
        for updates in (
            {"item_states": []},
            {"item_completion_reasons": []},
            {"item_failure_kinds": []},
            {"item_progress": []},
            {"item_metadata": []},
        ):
            value = self.upload(**updates)
            with self.subTest(updates=updates), self.assertRaises(job_store.JobStoreValidationError):
                job_store.serialize_job(value)
            self.assertIsNone(job_store.normalize_persisted_job(value))

    def test_safe_playlist_and_upload_metadata_are_retained(self):
        normalized = job_store.serialize_job(self.upload())
        self.assertEqual(normalized["playlist_id"], "PL_GLOBAL")
        self.assertEqual(
            normalized["item_metadata"][0]["youtube_playlist_id"],
            "PL_STREAMER",
        )
        self.assertEqual(normalized["item_metadata"][0]["vod_id"], "1234567890")

    def test_unknown_sensitive_runtime_fields_and_raw_errors_or_logs_never_serialize(self):
        sentinel_values = (
            "COOKIE_SECRET",
            "OAUTH_SECRET",
            "ACCESS_SECRET",
            "REFRESH_SECRET",
            "CLIENT_SECRET",
            "AUTH_SECRET",
            "COMMAND_SECRET",
            "MANIFEST_SECRET",
            "SIGNED_SECRET",
            "EXCEPTION_SECRET",
            "LOG_SECRET",
            "STDOUT_SECRET",
            "STDERR_SECRET",
            "ABSOLUTE_SECRET",
        )
        value = self.upload()
        value.update(
            {
                "cookies": sentinel_values[0],
                "oauth_token": sentinel_values[1],
                "access_token": sentinel_values[2],
                "refresh_token": sentinel_values[3],
                "client_secret": sentinel_values[4],
                "headers": {"Authorization": sentinel_values[5]},
                "authorization": sentinel_values[5],
                "command": [sentinel_values[6]],
                "manifest_url": f"https://invalid/{sentinel_values[7]}",
                "signed_url": f"https://invalid/{sentinel_values[8]}",
                "raw_exception": sentinel_values[9],
                "log": [sentinel_values[10]],
                "logs": [sentinel_values[10]],
                "stdout": sentinel_values[11],
                "stderr": sentinel_values[12],
                "absolute_host_path": f"C:/{sentinel_values[13]}",
                "process": object(),
                "thread": threading.Thread(),
                "some_future_runtime_object": object(),
                "item_errors": ["RAW_ITEM_ERROR_SECRET"],
            }
        )

        self.store.save([value], 3, revision=1)
        serialized = self.path.read_text(encoding="utf-8")

        for sentinel in (*sentinel_values, "RAW_ITEM_ERROR_SECRET"):
            self.assertNotIn(sentinel, serialized)
        for key in ("log", "logs", "stdout", "stderr", "command", "process"):
            self.assertNotIn(f'"{key}"', serialized)

    def test_pending_retry_sentinel_is_runtime_only_but_materialized_retry_is_kept(self):
        pending = job_store.serialize_job(
            self.download(item_retry_job_ids=["__pending__"])
        )
        linked = job_store.serialize_job(
            self.download(
                item_retry_job_ids=["9"],
                retry_of={"job_id": "8", "item_id": "8-item-1"},
            )
        )
        self.assertEqual(pending["item_retry_job_ids"], [""])
        self.assertEqual(linked["item_retry_job_ids"], ["9"])
        self.assertEqual(linked["retry_of"]["item_id"], "8-item-1")

    def test_atomic_save_produces_valid_json_and_cleans_temporary_files(self):
        self.store.save([self.download()], 2, revision=1)

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.store.load().state)
        self.assertEqual(list(self.dashboard_dir.glob("*.tmp")), [])
        self.assertEqual(list(self.dashboard_dir.glob(".*.tmp")), [])

    def test_replace_failure_preserves_previous_primary_and_cleans_temp(self):
        self.store.save([self.download()], 2, revision=1)
        original = self.path.read_bytes()
        with mock.patch.object(
            job_store.os, "replace", side_effect=OSError("simulated")
        ), self.assertRaises(job_store.JobStorePersistenceError):
            self.store.save([self.upload("2")], 3, revision=2)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.dashboard_dir.glob(".*.tmp")), [])
        self.assertEqual(self.store.status()["last_error_code"], "persistence_failed")

    def test_save_preserves_fatally_corrupt_existing_primary(self):
        self.dashboard_dir.mkdir(parents=True, exist_ok=True)
        damaged = b'{"secret":"KEEP_FOR_RECOVERY", broken'
        self.path.write_bytes(damaged)
        self.assertTrue(self.store.load().degraded)

        with self.assertRaises(job_store.JobStorePersistenceError):
            self.store.save([self.download()], 2, revision=1)

        self.assertEqual(self.path.read_bytes(), damaged)
        self.assertEqual(
            self.store.status()["last_error_code"],
            "corrupt_primary_preserved",
        )

    def test_unsafe_outgoing_job_fails_before_existing_primary_is_replaced(self):
        self.store.save([self.download()], 2, revision=1)
        original = self.path.read_bytes()
        unsafe = self.download(urls=["https://signed.invalid/SECRET"])

        with self.assertRaises(job_store.JobStoreValidationError):
            self.store.save([unsafe], 3, revision=2)

        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_revision_cannot_overwrite_newer_snapshot(self):
        newer = self.store.save([self.download("2")], 3, revision=10)
        stale = self.store.save([self.download("1")], 2, revision=9)

        self.assertTrue(newer.saved)
        self.assertFalse(stale.saved)
        self.assertTrue(stale.stale)
        self.assertEqual([job["id"] for job in self.store.load().jobs], ["2"])

    def test_equal_and_newer_revisions_save_normally(self):
        self.assertTrue(self.store.save([self.download("1")], 2, revision=1).saved)
        self.assertTrue(self.store.save([self.download("2")], 3, revision=1).saved)
        self.assertTrue(self.store.save([self.download("3")], 4, revision=2).saved)
        self.assertEqual(self.store.status()["last_written_revision"], 2)

    def test_concurrent_store_calls_leave_highest_revision_as_valid_json(self):
        failures = []

        def save_revision(revision):
            try:
                self.store.save(
                    [self.download(str(revision + 1))],
                    revision + 2,
                    revision=revision,
                )
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=save_revision, args=(value,)) for value in range(20)]
        for thread in reversed(threads):
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(self.store.status()["last_written_revision"], 19)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.store.load().state)

    def test_retention_keeps_all_nonterminal_and_only_newest_100_terminal(self):
        nonterminal = [
            self.download(str(index), state="running")
            for index in range(1, 121)
        ]
        terminal = []
        for index in range(121, 226):
            timestamp = self.now + timedelta(seconds=index)
            terminal.append(
                self.download(
                    str(index),
                    state="completed",
                    finished_at=timestamp.isoformat(),
                )
            )

        saved = self.store.save(nonterminal + terminal, 300, revision=1)
        retained = saved.state["jobs"]

        self.assertEqual(sum(job["state"] == "running" for job in retained), 120)
        terminal_ids = [int(job["id"]) for job in retained if job["state"] == "completed"]
        self.assertEqual(terminal_ids, list(range(126, 226)))
        self.assertEqual(saved.state["next_job_id"], 300)

    def test_retention_uses_timestamp_priority_and_numeric_id_fallback(self):
        values = [
            job_store.serialize_job(self.download("1", state="completed", created_at="2026-08-23T18:00:00Z")),
            job_store.serialize_job(self.download("2", state="completed", created_at="2026-08-23T18:00:00Z", updated_at="2026-08-23T18:05:00Z")),
            job_store.serialize_job(self.download("3", state="completed", created_at="2026-08-23T18:00:00Z", finished_at="2026-08-23T18:10:00Z")),
            job_store.serialize_job(self.download("4", state="completed", created_at="2026-08-23T18:00:00Z", finished_at="2026-08-23T18:10:00Z")),
        ]

        retained = job_store.apply_retention(values, terminal_limit=2)

        self.assertEqual([job["id"] for job in retained], ["3", "4"])

    def test_pruning_never_reduces_high_water_even_if_pruned_job_has_highest_id(self):
        jobs = []
        for index in range(1, 102):
            jobs.append(
                self.download(
                    str(index),
                    state="completed",
                    finished_at=(self.now + timedelta(seconds=index)).isoformat(),
                )
            )
        jobs.append(
            self.download(
                "999",
                state="completed",
                finished_at="2020-01-01T00:00:00Z",
            )
        )
        self.write_state(self.persisted_state(jobs, "invalid"))

        result = self.store.load()

        self.assertEqual(result.next_job_id, 1_000_000_000)
        self.assertNotIn("999", [job["id"] for job in result.jobs])

    def test_large_batch_is_retained_as_one_job_without_item_truncation(self):
        count = job_store.MAX_ITEMS_PER_JOB
        value = self.download(
            item_ids=[f"1-item-{index + 1}" for index in range(count)],
            item_states=["queued"] * count,
            item_completion_reasons=[""] * count,
            item_failure_kinds=[""] * count,
            item_resolved=[False] * count,
            item_retry_job_ids=[""] * count,
            urls=[f"https://www.twitch.tv/videos/{2000000000 + index}" for index in range(count)],
            total_urls=count,
            item_progress=[None] * count,
            item_processed_seconds=[None] * count,
            item_total_duration_seconds=[None] * count,
            item_updated_at=[None] * count,
        )

        normalized = job_store.serialize_job(value)

        self.assertEqual(len(normalized["item_ids"]), count)
        self.assertEqual(len(normalized["urls"]), count)

    def test_invalid_clock_revision_and_next_id_use_typed_validation_errors(self):
        naive_store = job_store.JobStore(self.path, clock=lambda: datetime(2026, 8, 23))
        for operation in (
            lambda: naive_store.save([], 1, revision=0),
            lambda: self.store.save([], 0, revision=0),
            lambda: self.store.save([], 1, revision=-1),
        ):
            with self.subTest(operation=operation), self.assertRaises(job_store.JobStoreValidationError):
                operation()

    def test_status_contains_only_safe_bounded_diagnostics(self):
        self.store.save([self.download()], 2, revision=4)
        status = self.store.status()

        self.assertEqual(
            set(status),
            {
                "healthy",
                "degraded",
                "source",
                "reason",
                "discarded_job_count",
                "last_save_at",
                "last_error_code",
                "last_written_revision",
            },
        )
        self.assertNotIn(str(self.path), json.dumps(status))

    def test_store_has_no_clear_completed_operation_in_p5b(self):
        self.assertFalse(hasattr(self.store, "clear_completed_history"))


if __name__ == "__main__":
    unittest.main()
