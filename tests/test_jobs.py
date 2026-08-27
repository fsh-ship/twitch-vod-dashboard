import os
import signal
import subprocess
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
    RECORDING_GRACEFUL_STOP_TIMEOUT_SECONDS,
    RECORDING_STOP_RESULT_GRACEFUL,
    RECORDING_STOP_RESULT_KILLED,
    RECORDING_TERMINATE_TIMEOUT_SECONDS,
    RecordingConflictError,
    RecordingWorkerDependencies,
    UploadWorkerDependencies,
    ffmpeg_download_metrics,
    download_process_group_options,
    parse_ffmpeg_speed_multiplier,
    parse_ffmpeg_time_seconds,
    run_download_job,
    run_recording_job,
    run_upload_job,
    terminate_download_process_tree,
    terminate_recording_process_tree,
)


class ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class DeferredThread(ImmediateThread):
    def start(self):
        self.started = True


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
                "state": "queued",
                "created": "2026-08-11 12:34:56",
                "created_at": "2026-08-11T12:34:56Z",
                "updated_at": "2026-08-11T12:34:56Z",
                "started_at": None,
                "finished_at": None,
                "urls": urls,
                "total_urls": 1,
                "item_ids": ["1-item-1"],
                "item_states": ["queued"],
                "item_statuses": ["wartet"],
                "item_progress": [None],
                "item_processed_seconds": [None],
                "item_speed_multiplier": [None],
                "item_speed_label": [""],
                "item_eta_seconds": [None],
                "item_updated_at": [None],
                "item_total_duration_seconds": [None],
                "item_resolved": [False],
                "item_failure_kinds": [""],
                "item_completion_reasons": [""],
                "item_recovery_reasons": [""],
                "item_retry_job_ids": [""],
                "stop_after_current": False,
                "completion_reason": "",
                "recovery_reason": "",
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
                "state": "queued",
                "created": "2026-08-11 12:34:56",
                "created_at": "2026-08-11T12:34:56Z",
                "updated_at": "2026-08-11T12:34:56Z",
                "started_at": None,
                "finished_at": None,
                "urls": ["C:/media/vod.mp4"],
                "item_ids": ["1-item-1"],
                "item_states": ["queued"],
                "item_statuses": ["wartet"],
                "item_progress": [None],
                "item_bytes_uploaded": [None],
                "item_total_bytes": [None],
                "item_bytes_per_second": [None],
                "item_eta_seconds": [None],
                "item_updated_at": [None],
                "item_errors": [""],
                "item_resolved": [False],
                "item_failure_kinds": [""],
                "item_completion_reasons": [""],
                "item_recovery_reasons": [""],
                "item_retry_job_ids": [""],
                "item_metadata": [{}],
                "stop_after_current": False,
                "completion_reason": "",
                "recovery_reason": "",
                "log": [],
                "returncode": None,
                "type": "youtube_upload",
            },
        )

    def test_recording_job_schema_has_exactly_one_item(self):
        manager = self.manager()

        job_id = manager.create_recording_job(
            "nika_livetv",
            stream_id="987654321",
            title="Actual stream title",
            live_started_at="2026-08-23T18:00:00Z",
            quality="source/best",
            output_name="nika_livetv/live-template.%(ext)s",
        )
        job = manager.jobs[job_id]

        self.assertEqual(job["type"], "recording")
        self.assertEqual(job["streamer"], "nika_livetv")
        self.assertEqual(job["origin"], "manual")
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(job["stream_id"], "987654321")
        self.assertEqual(job["title"], "Actual stream title")
        self.assertEqual(job["live_started_at"], "2026-08-23T18:00:00Z")
        self.assertEqual(job["quality"], "source/best")
        self.assertEqual(job["urls"], ["nika_livetv"])
        self.assertEqual(job["total_urls"], 1)
        self.assertEqual(job["item_ids"], ["1-item-1"])
        self.assertEqual(job["item_states"], ["queued"])
        self.assertEqual(job["recorded_seconds"], 0.0)
        self.assertEqual(job["completion_reason"], "")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["created_at"], "2026-08-11T12:34:56Z")
        self.assertEqual(job["updated_at"], "2026-08-11T12:34:56Z")
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["finished_at"])

    def test_second_pending_recording_is_rejected(self):
        manager = self.manager()
        manager.create_recording_job("nika_livetv")

        with self.assertRaisesRegex(RuntimeError, "already queued or active"):
            manager.create_recording_job("another_streamer")

    def test_recording_origin_and_attempt_are_validated_and_frozen(self):
        manager = self.manager()
        job_id = manager.create_recording_job(
            "nika_livetv", origin="auto", attempt=3
        )

        self.assertEqual(manager.jobs[job_id]["origin"], "auto")
        self.assertEqual(manager.jobs[job_id]["attempt"], 3)
        invalid_cases = (
            ({"origin": "scheduled"}, "invalid_origin"),
            ({"attempt": 0}, "invalid_attempt"),
            ({"attempt": 1001}, "invalid_attempt"),
            ({"attempt": True}, "invalid_attempt"),
            ({"attempt": "2"}, "invalid_attempt"),
        )
        for kwargs, expected in invalid_cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                ValueError, expected
            ):
                manager.create_recording_job("other_streamer", **kwargs)

    def test_concurrent_recording_creation_reserves_exactly_one_job(self):
        manager = self.manager()
        barrier = threading.Barrier(2)
        created = []
        conflicts = []
        result_lock = threading.Lock()

        def create_one(streamer):
            barrier.wait()
            try:
                job_id = manager.create_recording_job(streamer)
            except RecordingConflictError as exc:
                with result_lock:
                    conflicts.append(exc)
            else:
                with result_lock:
                    created.append(job_id)

        threads = [
            threading.Thread(target=create_one, args=(streamer,))
            for streamer in ("first_streamer", "second_streamer")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(created), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(manager.jobs), 1)

    def test_upload_jobs_store_their_explicit_playlist_independently(self):
        manager = self.manager()

        first_id = manager.create_upload_job(
            ["C:/media/one.mp4"], "First", playlist_id="playlist-a"
        )
        second_id = manager.create_upload_job(
            ["C:/media/two.mp4"], "Second", playlist_id="playlist-b"
        )

        self.assertEqual(manager.jobs[first_id]["playlist_id"], "playlist-a")
        self.assertEqual(manager.jobs[second_id]["playlist_id"], "playlist-b")

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

    def test_ffmpeg_time_and_speed_parsing_matches_runtime_output(self):
        line = (
            "frame=1585554 fps=3656 q=-1.0 size=25652480KiB "
            "time=07:20:36.46 bitrate=7949.1kbits/s speed=61x"
        )

        self.assertAlmostEqual(parse_ffmpeg_time_seconds(line), 26436.46)
        self.assertEqual(parse_ffmpeg_speed_multiplier(line), 61.0)

    def test_ffmpeg_metrics_use_known_duration_for_percent_and_eta(self):
        metrics = ffmpeg_download_metrics(
            "frame=1 time=00:12:00.00 speed=4x", 1000
        )

        self.assertEqual(metrics["processed_seconds"], 720.0)
        self.assertEqual(metrics["speed_multiplier"], 4.0)
        self.assertEqual(metrics["progress"], 72.0)
        self.assertEqual(metrics["eta_seconds"], 70.0)

    def test_ffmpeg_zero_or_invalid_speed_never_produces_eta(self):
        zero = ffmpeg_download_metrics(
            "frame=1 time=00:12:00.00 speed=0x", 1000
        )
        invalid = ffmpeg_download_metrics(
            "frame=1 time=00:12:00.00 speed=N/A", 1000
        )

        self.assertEqual(zero["speed_multiplier"], 0.0)
        self.assertIsNone(zero["eta_seconds"])
        self.assertIsNone(invalid["speed_multiplier"])
        self.assertIsNone(invalid["eta_seconds"])

    def test_ffmpeg_log_updates_running_item_from_extracted_duration(self):
        manager = JobManager(clock=lambda: 100.0)
        job_id = manager.create_download_job(["one"], "Download")
        manager.set_download_item_status(job_id, 1, "l\u00e4uft")

        manager.append_job_log(job_id, "VOD-DASHBOARD-DURATION=1000")
        manager.append_job_log(
            job_id, "frame=1 time=00:12:00.00 speed=4x"
        )

        job = manager.jobs[job_id]
        self.assertEqual(job["item_total_duration_seconds"], [1000.0])
        self.assertEqual(job["item_processed_seconds"], [720.0])
        self.assertEqual(job["item_speed_multiplier"], [4.0])
        self.assertEqual(job["item_speed_label"], ["4x"])
        self.assertEqual(job["item_progress"], [72.0])
        self.assertEqual(job["item_eta_seconds"], [70])
        self.assertEqual(job["item_updated_at"], [100.0])

    def test_ffmpeg_progress_without_duration_has_no_percent_or_eta(self):
        manager = JobManager(clock=lambda: 100.0)
        job_id = manager.create_download_job(["one"], "Download")
        manager.set_download_item_status(job_id, 1, "l\u00e4uft")

        manager.append_job_log(
            job_id, "frame=1 time=07:20:36.46 speed=61x"
        )

        job = manager.jobs[job_id]
        self.assertEqual(job["item_processed_seconds"], [26436.46])
        self.assertEqual(job["item_speed_label"], ["61x"])
        self.assertEqual(job["item_progress"], [None])
        self.assertEqual(job["item_eta_seconds"], [None])

    def test_classic_ytdlp_progress_remains_structured(self):
        manager = JobManager(clock=lambda: 100.0)
        job_id = manager.create_download_job(["one"], "Download")
        manager.set_download_item_status(job_id, 1, "l\u00e4uft")

        manager.append_job_log(
            job_id,
            "[download] 42.0% of 2.00GiB at 4.20MiB/s ETA 00:42",
        )

        job = manager.jobs[job_id]
        self.assertEqual(job["item_progress"], [42.0])
        self.assertEqual(job["item_speed_label"], ["4.20MiB/s"])
        self.assertEqual(job["item_eta_seconds"], [42])

    def test_completed_download_clears_active_progress_and_eta(self):
        manager = JobManager(clock=lambda: 100.0)
        job_id = manager.create_download_job(["one"], "Download")
        manager.set_download_item_status(job_id, 1, "l\u00e4uft")
        manager.append_job_log(job_id, "VOD-DASHBOARD-DURATION=1000")
        manager.append_job_log(
            job_id, "frame=1 time=00:12:00.00 speed=4x"
        )

        manager.set_download_item_status(job_id, 1, "fertig")

        job = manager.jobs[job_id]
        self.assertEqual(job["item_progress"], [None])
        self.assertEqual(job["item_processed_seconds"], [None])
        self.assertEqual(job["item_speed_multiplier"], [None])
        self.assertEqual(job["item_speed_label"], [""])
        self.assertEqual(job["item_eta_seconds"], [None])
        self.assertEqual(job["item_updated_at"], [None])

    def test_upload_progress_updates_only_the_explicitly_running_item(self):
        manager = self.manager()
        job_id = manager.create_upload_job(
            ["C:/media/one.mp4", "C:/media/two.mp4"], "Upload"
        )
        manager.start_job(job_id)
        manager.set_upload_item_status(job_id, 1, "\u006c\u00e4uft", progress=0)

        manager.append_job_log(job_id, "YouTube Upload one.mp4: 52%")

        self.assertEqual(manager.jobs[job_id]["item_progress"], [52, None])
        self.assertEqual(
            manager.jobs[job_id]["item_statuses"], ["\u006c\u00e4uft", "wartet"]
        )

    def test_active_upload_bytes_produce_speed_and_eta(self):
        manager = self.manager()
        job_id = manager.create_upload_job(["C:/media/one.mp4"], "Upload")
        manager.start_job(job_id)
        manager.set_upload_item_status(job_id, 1, "l\u00e4uft", progress=0)
        mib = 1024**2

        manager.update_active_upload_progress(
            job_id, 10 * mib, 100 * mib, observed_at=0
        )
        manager.update_active_upload_progress(
            job_id, 20 * mib, 100 * mib, observed_at=5
        )

        job = manager.jobs[job_id]
        self.assertEqual(job["item_progress"], [20.0])
        self.assertEqual(job["item_bytes_uploaded"], [20 * mib])
        self.assertEqual(job["item_total_bytes"], [100 * mib])
        self.assertEqual(job["item_bytes_per_second"], [2 * mib])
        self.assertEqual(job["item_eta_seconds"], [40])
        self.assertEqual(job["item_updated_at"], [5.0])

    def test_upload_eta_needs_two_transfer_samples(self):
        manager = self.manager()
        job_id = manager.create_upload_job(["C:/media/one.mp4"], "Upload")
        manager.start_job(job_id)
        manager.set_upload_item_status(job_id, 1, "l\u00e4uft", progress=0)

        manager.update_active_upload_progress(
            job_id, 10_000, 100_000, observed_at=1
        )

        job = manager.jobs[job_id]
        self.assertIsNone(job["item_bytes_per_second"][0])
        self.assertIsNone(job["item_eta_seconds"][0])

    def test_zero_upload_speed_never_produces_an_invalid_eta(self):
        manager = self.manager()
        job_id = manager.create_upload_job(["C:/media/one.mp4"], "Upload")
        manager.start_job(job_id)
        manager.set_upload_item_status(job_id, 1, "l\u00e4uft", progress=0)

        manager.update_active_upload_progress(
            job_id, 10_000, 100_000, observed_at=1
        )
        manager.update_active_upload_progress(
            job_id, 10_000, 100_000, observed_at=6
        )

        job = manager.jobs[job_id]
        self.assertEqual(job["item_bytes_per_second"], [0.0])
        self.assertEqual(job["item_eta_seconds"], [None])

    def test_upload_speed_uses_ema_to_smooth_fluctuating_samples(self):
        manager = self.manager()
        job_id = manager.create_upload_job(["C:/media/one.mp4"], "Upload")
        manager.start_job(job_id)
        manager.set_upload_item_status(job_id, 1, "l\u00e4uft", progress=0)
        mib = 1024**2

        manager.update_active_upload_progress(
            job_id, 0, 400 * mib, observed_at=0
        )
        manager.update_active_upload_progress(
            job_id, 100 * mib, 400 * mib, observed_at=10
        )
        manager.update_active_upload_progress(
            job_id, 110 * mib, 400 * mib, observed_at=20
        )

        job = manager.jobs[job_id]
        self.assertAlmostEqual(
            job["item_bytes_per_second"][0] / mib,
            7.3,
            places=2,
        )
        self.assertEqual(job["item_eta_seconds"], [40])

    def test_unfinished_upload_paths_and_resolved_error_state(self):
        manager = self.manager()
        job_id = manager.create_upload_job(
            ["C:/media/one.mp4", "C:/media/two.mp4"], "Upload"
        )
        manager.set_upload_item_status(job_id, 1, "fehler", error="failed")

        self.assertEqual(
            manager.unfinished_upload_paths(), {"C:/media/two.mp4"}
        )
        self.assertTrue(manager.resolve_error(job_id, 1))
        self.assertTrue(manager.jobs[job_id]["item_resolved"][0])
        self.assertEqual(manager.jobs[job_id]["item_statuses"][0], "fehler")
        self.assertEqual(manager.jobs[job_id]["item_errors"][0], "failed")

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


class QueueControlManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()

    def test_item_ids_are_immutable_across_state_and_presentation_changes(self):
        first = self.manager.create_download_job(["one", "two"], "First")
        second = self.manager.create_download_job(["three"], "Second")
        original = list(self.manager.jobs[first]["item_ids"])

        claim = self.manager.claim_next_item(first)
        self.manager.finish_claimed_item(first, claim["item_id"], "completed")
        newest_first = self.manager.snapshot_jobs(reverse=True)

        self.assertEqual(self.manager.jobs[first]["item_ids"], original)
        self.assertEqual([job["id"] for job in newest_first], [second, first])
        self.assertEqual(newest_first[1]["item_ids"], original)

    def test_worker_claims_next_item_from_manager(self):
        job_id = self.manager.create_download_job(["one", "two"], "Queue")

        first = self.manager.claim_next_item(job_id)
        self.manager.finish_claimed_item(job_id, first["item_id"], "completed")
        second = self.manager.claim_next_item(job_id)

        self.assertEqual(first["value"], "one")
        self.assertEqual(second["value"], "two")
        self.assertNotEqual(first["item_id"], second["item_id"])

    def test_recording_lane_does_not_block_download_lane(self):
        download_id = self.manager.create_download_job(["vod"], "Download")
        recording_id = self.manager.create_recording_job("nika_livetv")

        download = self.manager.claim_next_item(download_id)
        recording = self.manager.claim_recording_job(recording_id)

        self.assertEqual(download["lane"], "download")
        self.assertEqual(recording["lane"], "recording")
        self.assertTrue(self.manager.is_recording_active())
        self.assertEqual(
            self.manager.snapshot_jobs()[1]["lane"], "recording"
        )
        self.assertTrue(
            self.manager.queue_controls_snapshot()["download"][
                "has_active_item"
            ]
        )

    def test_internal_recording_stop_flag_uses_stopping_state(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        claimed = self.manager.claim_recording_job(job_id)

        self.assertTrue(self.manager.request_recording_stop(job_id))
        self.assertTrue(self.manager.is_recording_stop_requested(job_id))
        self.assertEqual(
            self.manager.item_state(job_id, claimed["item_id"]), "stopping"
        )
        self.assertEqual(self.manager.jobs[job_id]["state"], "stopping")
        self.assertTrue(self.manager.jobs[job_id]["stop_requested"])

    def test_recording_stop_before_process_registration_is_not_lost(self):
        job_id = self.manager.create_recording_job("nika_livetv")

        self.assertTrue(self.manager.request_recording_stop(job_id))
        claimed = self.manager.claim_recording_job(job_id)
        process = mock.Mock()

        self.assertEqual(claimed["lane"], "recording")
        self.assertEqual(
            self.manager.item_state(job_id, claimed["item_id"]), "stopping"
        )
        self.assertTrue(
            self.manager.register_recording_process(
                job_id, claimed["item_id"], process
            )
        )

    def test_worker_shutdown_reuses_recording_stop_and_waits_for_terminal(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        claimed = self.manager.claim_recording_job(job_id)

        def finalize(_job_id):
            self.manager.finalize_recording_job(
                job_id,
                claimed["item_id"],
                state="failed",
                returncode=-2,
                completion_reason="stop_incomplete",
            )
            return True

        with mock.patch.object(
            self.manager,
            "start_recording_termination",
            side_effect=finalize,
        ) as terminate:
            self.assertTrue(
                self.manager.stop_recording_for_shutdown(timeout=0.1)
            )

        self.assertTrue(self.manager.jobs[job_id]["stop_requested"])
        terminate.assert_called_once_with(job_id)

    def test_worker_shutdown_wait_is_bounded(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        with mock.patch.object(
            self.manager,
            "start_recording_termination",
            return_value=False,
        ):
            self.assertFalse(
                self.manager.stop_recording_for_shutdown(timeout=0)
            )
        self.assertTrue(self.manager.jobs[job_id]["stop_requested"])

    def test_recording_termination_thread_is_deduplicated(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        claimed = self.manager.claim_recording_job(job_id)
        process = mock.Mock()
        self.manager.register_recording_process(
            job_id, claimed["item_id"], process
        )
        self.manager.request_recording_stop(job_id)
        terminator = mock.Mock(return_value=RECORDING_STOP_RESULT_GRACEFUL)
        thread_factory = mock.Mock(side_effect=ImmediateThread)

        self.assertTrue(
            self.manager.start_recording_termination(
                job_id,
                terminator=terminator,
                thread_factory=thread_factory,
            )
        )
        self.assertTrue(
            self.manager.start_recording_termination(
                job_id,
                terminator=terminator,
                thread_factory=thread_factory,
            )
        )

        thread_factory.assert_called_once()
        terminator.assert_called_once()
        self.assertEqual(
            self.manager.wait_recording_stop_result(job_id),
            RECORDING_STOP_RESULT_GRACEFUL,
        )

    def test_recording_termination_scheduling_does_not_run_grace_wait_inline(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        claimed = self.manager.claim_recording_job(job_id)
        self.manager.register_recording_process(
            job_id, claimed["item_id"], mock.Mock()
        )
        self.manager.request_recording_stop(job_id)
        terminator = mock.Mock()

        self.assertTrue(
            self.manager.start_recording_termination(
                job_id,
                terminator=terminator,
                thread_factory=DeferredThread,
            )
        )

        terminator.assert_not_called()

    def test_public_queue_controls_do_not_operate_on_recording_items(self):
        job_id = self.manager.create_recording_job("nika_livetv")
        item_id = self.manager.jobs[job_id]["item_ids"][0]

        self.assertFalse(self.manager.remove_queued_item(job_id, item_id))
        claimed = self.manager.claim_recording_job(job_id)
        self.assertIsNone(
            self.manager.request_cancel_item(job_id, claimed["item_id"])
        )
        self.assertFalse(
            self.manager.request_stop_after_current(job_id, claimed["item_id"])
        )
        capabilities = self.manager.snapshot_jobs()[0]["item_capabilities"][0]
        self.assertFalse(capabilities["can_cancel"])
        self.assertFalse(capabilities["can_remove"])
        self.assertFalse(capabilities["can_retry"])
        self.assertFalse(capabilities["can_resolve"])
        self.assertFalse(capabilities["can_stop_after_current"])
        self.assertEqual(
            capabilities["retry_blocked_reason"],
            "recording_retry_unsupported",
        )

    def test_backend_exposes_only_legal_item_capabilities(self):
        job_id = self.manager.create_download_job(["one"], "Queue")
        queued = self.manager.snapshot_jobs()[0]["item_capabilities"][0]
        claim = self.manager.claim_next_item(job_id)
        running = self.manager.snapshot_jobs()[0]["item_capabilities"][0]

        self.assertTrue(queued["can_remove"])
        self.assertFalse(queued["can_cancel"])
        self.assertTrue(running["can_cancel"])
        self.assertTrue(running["can_stop_after_current"])
        self.assertFalse(running["can_remove"])
        self.manager.finish_claimed_item(job_id, claim["item_id"], "failed")
        failed = self.manager.snapshot_jobs()[0]["item_capabilities"][0]
        self.assertTrue(failed["can_retry"])

    def test_pause_allows_active_item_to_finish_and_resume_releases_next(self):
        job_id = self.manager.create_download_job(["one", "two"], "Queue")
        first = self.manager.claim_next_item(job_id)
        self.assertTrue(self.manager.pause_queue("download"))
        self.manager.finish_claimed_item(job_id, first["item_id"], "completed")
        claimed = []
        claimed_event = threading.Event()

        def claim_waiting():
            claimed.append(self.manager.claim_next_item(job_id))
            claimed_event.set()

        thread = threading.Thread(target=claim_waiting)
        thread.start()
        self.assertFalse(claimed_event.wait(0.1))
        self.assertEqual(self.manager.jobs[job_id]["item_states"], ["completed", "queued"])

        self.assertTrue(self.manager.resume_queue("download"))
        self.assertTrue(claimed_event.wait(1))
        thread.join(1)
        self.assertEqual(claimed[0]["value"], "two")

    def test_stop_after_current_gates_lane_until_resume(self):
        job_id = self.manager.create_download_job(["one", "two"], "Queue")
        first = self.manager.claim_next_item(job_id)
        self.assertTrue(
            self.manager.request_stop_after_current(job_id, first["item_id"])
        )
        self.manager.finish_claimed_item(job_id, first["item_id"], "completed")
        claimed_event = threading.Event()

        def claim_waiting():
            self.manager.claim_next_item(job_id)
            claimed_event.set()

        thread = threading.Thread(target=claim_waiting)
        thread.start()
        self.assertFalse(claimed_event.wait(0.1))
        controls = self.manager.queue_controls_snapshot()["download"]
        self.assertTrue(controls["queue_paused"])
        self.assertTrue(controls["stop_after_current"])

        self.manager.resume_queue("download")
        self.assertTrue(claimed_event.wait(1))
        thread.join(1)

    def test_remove_waiting_marks_cancelled_and_preserves_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "vod.mp4"
            video.write_bytes(b"video")
            job_id = self.manager.create_upload_job([str(video)], "Upload")
            item_id = self.manager.jobs[job_id]["item_ids"][0]

            self.assertTrue(self.manager.remove_queued_item(job_id, item_id))

            self.assertTrue(video.exists())
            self.assertEqual(self.manager.jobs[job_id]["item_states"], ["cancelled"])
            self.assertNotEqual(self.manager.jobs[job_id]["item_states"][0], "failed")
            self.assertIsNone(self.manager.claim_next_item(job_id))

    def test_retry_reservation_is_idempotent_and_preserves_original_failure(self):
        job_id = self.manager.create_download_job(["vod-url"], "Download")
        item_id = self.manager.jobs[job_id]["item_ids"][0]
        self.manager.set_download_item_status(job_id, 1, "fehler")

        first = self.manager.reserve_retry(job_id, item_id)
        second = self.manager.reserve_retry(job_id, item_id)
        self.manager.finalize_retry(job_id, item_id, "retry-2")
        third = self.manager.reserve_retry(job_id, item_id)

        self.assertTrue(first["reserved"])
        self.assertTrue(second["pending"])
        self.assertEqual(third["retry_job_id"], "retry-2")
        self.assertEqual(self.manager.jobs[job_id]["item_states"], ["failed"])

    def test_uncertain_upload_failure_blocks_fresh_retry(self):
        job_id = self.manager.create_upload_job(["vod.mp4"], "Upload")
        item_id = self.manager.jobs[job_id]["item_ids"][0]
        self.manager.finish_claimed_item(
            job_id, item_id, "failed", failure_kind="uncertain"
        )

        retry = self.manager.reserve_retry(job_id, item_id)

        self.assertTrue(retry["blocked"])
        self.assertIn("YouTube Studio", retry["reason"])

    def test_upload_cancel_transitions_cancelling_then_cancelled_at_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "vod.mp4"
            video.write_bytes(b"video")
            job_id = self.manager.create_upload_job([str(video)], "Upload")
            item_id = self.manager.jobs[job_id]["item_ids"][0]
            entered = threading.Event()
            release_chunk = threading.Event()

            def upload(*_args, **_kwargs):
                entered.set()
                release_chunk.wait(1)
                if self.manager.is_cancel_requested(job_id, item_id):
                    raise RuntimeError("cancelled at chunk boundary")
                return "video-id"

            dependencies = UploadWorkerDependencies(
                load_settings=lambda: {
                    "youtube_enabled": True,
                    "youtube_privacy_status": "private",
                    "youtube_playlist_id": "",
                },
                append_log=self.manager.append_job_log,
                get_youtube_service=mock.Mock(),
                safe_local_video_path=lambda raw, _settings: Path(raw),
                upload_to_youtube=upload,
            )
            worker = threading.Thread(
                target=run_upload_job,
                args=(job_id, self.manager, dependencies),
            )
            worker.start()
            self.assertTrue(entered.wait(1))

            self.assertEqual(
                self.manager.request_cancel_item(job_id, item_id),
                "youtube_upload",
            )
            self.assertEqual(self.manager.item_state(job_id, item_id), "cancelling")
            release_chunk.set()
            worker.join(1)

            self.assertFalse(worker.is_alive())
            self.assertEqual(self.manager.item_state(job_id, item_id), "cancelled")

    def test_upload_lane_pause_blocks_automatic_controller_job(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "auto.mp4"
            video.write_bytes(b"video")
            job_id = self.manager.create_upload_job(
                [str(video)], "Automatic YouTube Upload"
            )
            upload_started = threading.Event()
            dependencies = UploadWorkerDependencies(
                load_settings=lambda: {
                    "youtube_enabled": True,
                    "youtube_privacy_status": "private",
                    "youtube_playlist_id": "",
                },
                append_log=self.manager.append_job_log,
                get_youtube_service=mock.Mock(),
                safe_local_video_path=lambda raw, _settings: Path(raw),
                upload_to_youtube=lambda *_args, **_kwargs: (
                    upload_started.set() or "video-id"
                ),
            )
            self.manager.pause_queue("youtube_upload")
            worker = threading.Thread(
                target=run_upload_job,
                args=(job_id, self.manager, dependencies),
            )
            worker.start()
            self.assertFalse(upload_started.wait(0.1))

            self.manager.resume_queue("youtube_upload")
            self.assertTrue(upload_started.wait(1))
            worker.join(1)


class RecordingWorkerTests(unittest.TestCase):
    OUTPUT_MARKER = "VOD-DASHBOARD-RECORDING-FILE="

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.manager = JobManager(
            now=lambda: datetime(2026, 8, 23, 20, 0, 0)
        )
        self.settings = {
            "quality": "source/best",
            "merge_format": "mp4",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_job(self, *, attempt=1):
        return self.manager.create_recording_job(
            "nika_livetv",
            stream_id="987654321",
            title="Live title",
            live_started_at="2026-08-23T18:00:00Z",
            quality="1080p60/source/best",
            output_name="nika_livetv/live-template.%(ext)s",
            origin="auto" if attempt > 1 else "manual",
            attempt=attempt,
        )

    def dependencies(
        self,
        job_id,
        lines,
        returncode=0,
        stop_result=RECORDING_STOP_RESULT_GRACEFUL,
    ):
        process_calls = []

        def build_command(streamer, settings, *, attempt=1):
            self.assertEqual(streamer, "nika_livetv")
            self.assertEqual(settings["quality"], "1080p60/source/best")
            self.assertEqual(attempt, self.manager.jobs[job_id]["attempt"])
            return ["python", "-m", "yt_dlp", streamer]

        def resolve_output(raw, _settings):
            path = Path(raw).resolve()
            path.relative_to(self.root)
            if not path.is_file() or path.suffix.lower() != ".mp4":
                raise RuntimeError("Incomplete recording output.")
            return path.relative_to(self.root).as_posix()

        def popen(command, **kwargs):
            self.assertIn(
                self.manager.jobs[job_id]["state"], {"running", "stopping"}
            )
            process = mock.Mock(pid=4321)

            def output():
                self.assertIs(
                    self.manager.recording_process(job_id), process
                )
                yield from lines

            process.stdout = output()
            process.wait.return_value = returncode
            process_calls.append((command, kwargs, process))
            return process

        return RecordingWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            build_recording_command=build_command,
            download_directory=lambda _settings: self.root,
            resolve_completed_output=resolve_output,
            output_marker=self.OUTPUT_MARKER,
            popen=popen,
            terminate_process=mock.Mock(return_value=stop_result),
            thread_factory=ImmediateThread,
        ), process_calls

    def test_success_tracks_duration_process_and_safe_final_output(self):
        job_id = self.create_job()
        video = self.root / "nika_livetv" / "recording.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")
        lines = [
            "Opening https://signed.invalid/segment.ts?token=SECRET\n",
            "frame=1 time=00:12:34.50 speed=1x\n",
            "Useful recorder message\n",
            f"{self.OUTPUT_MARKER}{video}\n",
        ]
        dependencies, process_calls = self.dependencies(job_id, lines)

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["item_states"], ["completed"])
        self.assertEqual(job["returncode"], 0)
        self.assertEqual(job["completion_reason"], "natural_end")
        self.assertEqual(job["recorded_seconds"], 754.5)
        self.assertEqual(
            job["output_path"], "nika_livetv/recording.mp4"
        )
        self.assertTrue(job["output_complete"])
        self.assertIsNone(self.manager.recording_process(job_id))
        self.assertFalse(self.manager.is_recording_active())
        self.assertIn("Useful recorder message", job["log"])
        serialized_log = "\n".join(job["log"])
        self.assertNotIn("signed.invalid", serialized_log)
        self.assertNotIn("SECRET", serialized_log)
        self.assertNotIn("time=", serialized_log)

        command, kwargs, _process = process_calls[0]
        self.assertEqual(command, ["python", "-m", "yt_dlp", "nika_livetv"])
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertNotIn("shell", kwargs)
        for key, value in download_process_group_options().items():
            self.assertEqual(kwargs[key], value)

    def test_retry_attempt_reaches_recording_command_builder(self):
        job_id = self.create_job(attempt=2)
        dependencies, process_calls = self.dependencies(
            job_id, [], returncode=9
        )
        run_recording_job(job_id, self.manager, dependencies)
        self.assertEqual(self.manager.jobs[job_id]["attempt"], 2)
        self.assertEqual(len(process_calls), 1)

    def test_nonzero_returncode_fails_and_releases_process_reference(self):
        job_id = self.create_job()
        dependencies, _ = self.dependencies(
            job_id, ["ordinary diagnostic\n"], returncode=9
        )

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["returncode"], 9)
        self.assertEqual(job["completion_reason"], "process_error")
        self.assertFalse(job["output_complete"])
        self.assertIsNone(self.manager.recording_process(job_id))
        self.assertFalse(self.manager.is_recording_active())

    def test_part_file_marker_is_never_reported_as_completed_output(self):
        job_id = self.create_job()
        partial = self.root / "nika_livetv" / "recording.mp4.part"
        partial.parent.mkdir()
        partial.write_bytes(b"partial")
        dependencies, _ = self.dependencies(
            job_id, [f"{self.OUTPUT_MARKER}{partial}\n"]
        )

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "completed")
        self.assertIsNone(job["output_path"])
        self.assertFalse(job["output_complete"])

    def test_recording_logs_remain_capped_at_500(self):
        job_id = self.create_job()
        dependencies, _ = self.dependencies(
            job_id, [f"diagnostic-{index}\n" for index in range(550)]
        )

        run_recording_job(job_id, self.manager, dependencies)

        logs = self.manager.jobs[job_id]["log"]
        self.assertEqual(len(logs), 500)
        self.assertEqual(logs[-1], "Recording completed naturally.")

    def test_user_stop_with_final_output_completes_without_failed_state(self):
        job_id = self.create_job()
        video = self.root / "nika_livetv" / "stopped.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")

        def request_stop():
            self.assertTrue(self.manager.request_recording_stop(job_id))
            yield "frame=1 time=00:00:12.00 speed=1x\n"
            yield f"{self.OUTPUT_MARKER}{video}\n"

        dependencies, _ = self.dependencies(
            job_id, request_stop(), returncode=130
        )

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["completion_reason"], "stopped_by_user")
        self.assertEqual(job["returncode"], 130)
        self.assertEqual(job["recorded_seconds"], 12.0)
        self.assertTrue(job["output_complete"])
        self.assertEqual(job["output_path"], "nika_livetv/stopped.mp4")

    def test_stop_requested_before_registration_stops_new_process(self):
        job_id = self.create_job()
        video = self.root / "nika_livetv" / "early-stop.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")
        self.assertTrue(self.manager.request_recording_stop(job_id))
        dependencies, _ = self.dependencies(
            job_id,
            [f"{self.OUTPUT_MARKER}{video}\n"],
            returncode=130,
        )

        run_recording_job(job_id, self.manager, dependencies)

        dependencies.terminate_process.assert_called_once()
        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["completion_reason"], "stopped_by_user")

    def test_user_stop_without_final_output_is_conservatively_failed(self):
        job_id = self.create_job()

        def request_stop():
            self.manager.request_recording_stop(job_id)
            yield "frame=1 time=00:00:12.00 speed=1x\n"

        dependencies, _ = self.dependencies(
            job_id, request_stop(), returncode=130
        )

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["completion_reason"], "stop_incomplete")
        self.assertFalse(job["output_complete"])
        self.assertIsNone(job["output_path"])

    def test_hard_kill_is_never_reported_as_successful_user_stop(self):
        job_id = self.create_job()
        video = self.root / "nika_livetv" / "forced.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")

        def request_stop():
            self.manager.request_recording_stop(job_id)
            yield f"{self.OUTPUT_MARKER}{video}\n"

        dependencies, _ = self.dependencies(
            job_id,
            request_stop(),
            returncode=-9,
            stop_result=RECORDING_STOP_RESULT_KILLED,
        )

        run_recording_job(job_id, self.manager, dependencies)

        job = self.manager.jobs[job_id]
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["completion_reason"], "stop_failed")
        self.assertFalse(job["output_complete"])
        self.assertIsNone(self.manager.recording_process(job_id))


class DownloadProcessControlTests(unittest.TestCase):
    def test_posix_downloads_start_in_dedicated_session(self):
        self.assertEqual(
            download_process_group_options("posix"),
            {"start_new_session": True},
        )

    def test_graceful_download_cancel_signals_group_and_reaps_process(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.return_value = 0

        with mock.patch(
            "vod_dashboard.jobs.os.getpgid", return_value=9876, create=True
        ), mock.patch(
            "vod_dashboard.jobs.os.killpg", create=True
        ) as kill_group:
            terminate_download_process_tree(
                process, platform_name="posix", graceful_timeout=0.01
            )

        kill_group.assert_called_once_with(9876, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=0.01)

    def test_stubborn_download_cancel_escalates_entire_group(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            0,
        ]

        with mock.patch(
            "vod_dashboard.jobs.os.getpgid", return_value=9876, create=True
        ), mock.patch(
            "vod_dashboard.jobs.os.killpg", create=True
        ) as kill_group:
            terminate_download_process_tree(
                process,
                platform_name="posix",
                graceful_timeout=0.01,
                terminate_timeout=0.01,
            )

        self.assertEqual(
            kill_group.call_args_list,
            [
                mock.call(9876, signal.SIGINT),
                mock.call(9876, signal.SIGTERM),
                mock.call(9876, getattr(signal, "SIGKILL", 9)),
            ],
        )
        self.assertEqual(process.wait.call_count, 3)


class RecordingProcessControlTests(unittest.TestCase):
    def test_recording_stop_uses_dedicated_generous_timeouts(self):
        self.assertEqual(RECORDING_GRACEFUL_STOP_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(RECORDING_TERMINATE_TIMEOUT_SECONDS, 15.0)

    def test_posix_recording_stop_sends_sigint_to_process_group(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.return_value = 0

        with mock.patch(
            "vod_dashboard.jobs.os.getpgid", return_value=9876, create=True
        ), mock.patch(
            "vod_dashboard.jobs.os.killpg", create=True
        ) as kill_group:
            result = terminate_recording_process_tree(
                process, platform_name="posix", graceful_timeout=0.01
            )

        self.assertEqual(result, RECORDING_STOP_RESULT_GRACEFUL)
        kill_group.assert_called_once_with(9876, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=0.01)

    def test_posix_recording_stop_escalates_to_sigkill(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            0,
        ]
        messages = []

        with mock.patch(
            "vod_dashboard.jobs.os.getpgid", return_value=9876, create=True
        ), mock.patch(
            "vod_dashboard.jobs.os.killpg", create=True
        ) as kill_group:
            result = terminate_recording_process_tree(
                process,
                platform_name="posix",
                graceful_timeout=0.01,
                terminate_timeout=0.01,
                log_callback=messages.append,
            )

        self.assertEqual(result, RECORDING_STOP_RESULT_KILLED)
        self.assertEqual(
            kill_group.call_args_list,
            [
                mock.call(9876, signal.SIGINT),
                mock.call(9876, signal.SIGTERM),
                mock.call(9876, getattr(signal, "SIGKILL", 9)),
            ],
        )
        self.assertEqual(
            messages,
            [
                "Recording stop escalated to SIGTERM.",
                "Recording stop escalated to SIGKILL.",
            ],
        )

    def test_windows_recording_stop_escalates_taskkill_then_force(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            subprocess.TimeoutExpired("yt-dlp", 0.01),
            0,
        ]
        messages = []

        with mock.patch.object(
            signal, "CTRL_BREAK_EVENT", 987, create=True
        ), mock.patch("vod_dashboard.jobs.subprocess.run") as taskkill:
            result = terminate_recording_process_tree(
                process,
                platform_name="nt",
                graceful_timeout=0.01,
                terminate_timeout=0.01,
                log_callback=messages.append,
            )

        self.assertEqual(result, RECORDING_STOP_RESULT_KILLED)
        process.send_signal.assert_called_once_with(987)
        self.assertEqual(
            taskkill.call_args_list,
            [
                mock.call(
                    ["taskkill", "/PID", "4321", "/T"],
                    capture_output=True,
                    timeout=0.01,
                    check=False,
                ),
                mock.call(
                    ["taskkill", "/PID", "4321", "/T", "/F"],
                    capture_output=True,
                    timeout=0.01,
                    check=False,
                ),
            ],
        )
        self.assertEqual(
            messages,
            [
                "Recording stop escalated to taskkill /T.",
                "Recording stop escalated to taskkill /T /F.",
            ],
        )


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
        enqueue=None,
        stdout_lines=None,
        auto_result=None,
        settings_loader=None,
        auto_youtube_admission=None,
        auto_youtube_admit=None,
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
            process.stdout = list(stdout_lines or ["yt-dlp output\n"])
            process.wait.return_value = next(returncodes)
            process_calls.append((command, kwargs, process))
            return process

        snapshots = iter(({"old.mp4": 1.0}, {"old.mp4": 1.0, "new.mp4": 2.0}) * 10)
        candidate_paths = list(candidates)
        dependencies = DownloadWorkerDependencies(
            load_settings=settings_loader or (lambda: dict(self.settings)),
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
            enqueue_upload_job=enqueue,
            resolve_auto_vod_completed_output=auto_result,
            auto_youtube_admission_decision=auto_youtube_admission,
            admit_auto_youtube_intent=auto_youtube_admit,
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
        enqueue = mock.Mock(return_value="upload-2")
        dependencies, _, _ = self.dependencies(
            candidates=videos,
            prepare=prepare,
            service=service,
            upload=upload,
            enqueue=enqueue,
        )

        run_download_job(job_id, self.manager, dependencies)

        service.assert_called_once_with(self.settings, interactive=False)
        self.assertEqual(prepare.call_count, 2)
        upload.assert_not_called()
        enqueue.assert_called_once_with(
            [str(videos[0])], "Automatic YouTube Upload"
        )
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

    def test_auto_vod_download_only_never_enters_youtube_path(self):
        self.settings["youtube_enabled"] = True
        self.settings["youtube_auto_upload"] = True
        job_id = self.manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"],
            "Automatic Twitch VOD: example_streamer",
            origin="auto_vod",
            streamer="example_streamer",
            twitch_vod_id="1234567890",
            attempt=1,
            post_download_mode="download_only",
        )
        video = self.root / "new.mp4"
        prepare = mock.Mock(return_value=video)
        service = mock.Mock()
        upload = mock.Mock()
        enqueue = mock.Mock()
        dependencies, _, _ = self.dependencies(
            candidates=[video],
            prepare=prepare,
            service=service,
            upload=upload,
            enqueue=enqueue,
            stdout_lines=[
                "VOD-DASHBOARD-FINAL-FILE=/temporary/example_streamer/video.mp4\n"
            ],
            auto_result=lambda *_args: {
                "completed_media_path": "example_streamer/video.mp4",
                "completed_media_size_bytes": 123,
                "completed_twitch_vod_id": "1234567890",
            },
        )

        run_download_job(job_id, self.manager, dependencies)

        prepare.assert_not_called()
        service.assert_not_called()
        upload.assert_not_called()
        enqueue.assert_not_called()
        dependencies.new_video_files.assert_not_called()
        dependencies.recently_changed_video_files.assert_not_called()
        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(
            self.manager.jobs[job_id]["completed_media_path"],
            "example_streamer/video.mp4",
        )
        self.assertIn("download-only", "\n".join(self.manager.jobs[job_id]["log"]))

    def test_auto_youtube_uses_settings_reloaded_at_auto_vod_completion(self):
        job_id = self.manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"],
            "Automatic Twitch VOD: example_streamer",
            origin="auto_vod",
            streamer="example_streamer",
            twitch_vod_id="1234567890",
            attempt=1,
            post_download_mode="download_only",
        )
        settings_at_start = {
            **self.settings,
            "auto_youtube_enabled": True,
            "streamer_profiles": {
                "example_streamer": {"auto_youtube_upload": True}
            },
        }
        settings_at_completion = {
            **settings_at_start,
            "auto_youtube_enabled": False,
        }
        settings_loader = mock.Mock(
            side_effect=[settings_at_start, settings_at_completion]
        )
        admission = mock.Mock(
            side_effect=lambda settings, _streamer: {
                "handoff": (
                    "intent_pending"
                    if settings["auto_youtube_enabled"]
                    else "not_eligible"
                ),
                "reason": "" if settings["auto_youtube_enabled"] else "global_disabled",
                "playlist_id": "",
            }
        )
        admit = mock.Mock()
        dependencies, _, _ = self.dependencies(
            stdout_lines=[
                "VOD-DASHBOARD-FINAL-FILE=/temporary/example_streamer/video.mp4\n"
            ],
            auto_result=lambda *_args: {
                "completed_media_path": "example_streamer/video.mp4",
                "completed_media_size_bytes": 123,
                "completed_twitch_vod_id": "1234567890",
            },
            settings_loader=settings_loader,
            auto_youtube_admission=admission,
            auto_youtube_admit=admit,
        )

        run_download_job(job_id, self.manager, dependencies)

        admission.assert_called_once_with(
            settings_at_completion, "example_streamer"
        )
        admit.assert_not_called()
        self.assertEqual(
            self.manager.jobs[job_id]["item_auto_youtube_handoffs"],
            ["not_eligible"],
        )

    def test_auto_youtube_can_admit_when_enabled_before_completion(self):
        job_id = self.manager.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"],
            "Automatic Twitch VOD: example_streamer",
            origin="auto_vod",
            streamer="example_streamer",
            twitch_vod_id="1234567890",
            attempt=1,
            post_download_mode="download_only",
        )
        disabled = {**self.settings, "auto_youtube_enabled": False}
        enabled = {
            **disabled,
            "auto_youtube_enabled": True,
            "streamer_profiles": {
                "example_streamer": {"auto_youtube_upload": True}
            },
        }
        admit = mock.Mock()
        dependencies, _, _ = self.dependencies(
            stdout_lines=[
                "VOD-DASHBOARD-FINAL-FILE=/temporary/example_streamer/video.mp4\n"
            ],
            auto_result=lambda *_args: {
                "completed_media_path": "example_streamer/video.mp4",
                "completed_media_size_bytes": 123,
                "completed_twitch_vod_id": "1234567890",
            },
            settings_loader=mock.Mock(side_effect=[disabled, enabled]),
            auto_youtube_admission=lambda settings, _streamer: {
                "handoff": (
                    "intent_pending"
                    if settings["auto_youtube_enabled"]
                    else "not_eligible"
                ),
                "reason": "" if settings["auto_youtube_enabled"] else "global_disabled",
                "playlist_id": "PLAYLIST_AT_COMPLETION",
            },
            auto_youtube_admit=admit,
        )

        run_download_job(job_id, self.manager, dependencies)

        admit.assert_called_once_with(job_id, "1-item-1", enabled)
        self.assertEqual(
            self.manager.jobs[job_id]["item_auto_youtube_handoffs"],
            ["intent_pending"],
        )
        self.assertEqual(
            self.manager.jobs[job_id]["item_auto_youtube_playlist_ids"],
            ["PLAYLIST_AT_COMPLETION"],
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

    def test_cancelled_download_finishes_cancelled_not_failed(self):
        job_id = self.create_job()
        item_id = self.manager.jobs[job_id]["item_ids"][0]
        dependencies, _, _ = self.dependencies()
        process = mock.Mock()

        def output():
            self.manager.request_cancel_item(job_id, item_id)
            yield "download interrupted\n"

        process.stdout = output()
        process.wait.return_value = -2
        dependencies = DownloadWorkerDependencies(
            **{**dependencies.__dict__, "popen": mock.Mock(return_value=process)}
        )

        run_download_job(job_id, self.manager, dependencies)

        self.assertEqual(self.manager.jobs[job_id]["item_states"], ["cancelled"])
        self.assertNotEqual(self.manager.jobs[job_id]["state"], "failed")
        self.assertIn(
            "VOD 1/1 download cancelled. Partial files were retained.",
            self.manager.jobs[job_id]["log"],
        )


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

    def run_worker(
        self,
        paths,
        *,
        safe=None,
        service=None,
        upload=None,
        playlist_id=None,
    ):
        job_id = self.manager.create_upload_job(
            [str(path) for path in paths],
            "Upload",
            playlist_id=playlist_id,
        )
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            get_youtube_service=service or mock.Mock(),
            safe_local_video_path=safe or mock.Mock(side_effect=lambda raw, settings: Path(raw)),
            upload_to_youtube=upload or mock.Mock(return_value="video-id"),
        )
        run_upload_job(job_id, self.manager, dependencies)
        return job_id, dependencies

    def test_job_playlist_overrides_a_later_global_setting_change(self):
        video = self.root / "one.mp4"
        upload = mock.Mock(return_value="id-1")
        job_id = self.manager.create_upload_job(
            [str(video)], "Upload", playlist_id="playlist-original"
        )
        self.settings["youtube_playlist_id"] = "playlist-changed"
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            get_youtube_service=mock.Mock(),
            safe_local_video_path=mock.Mock(
                side_effect=lambda raw, settings: Path(raw)
            ),
            upload_to_youtube=upload,
        )

        run_upload_job(job_id, self.manager, dependencies)

        upload_settings = upload.call_args.args[1]
        self.assertEqual(job_id, "1")
        self.assertEqual(
            upload_settings["youtube_playlist_id"], "playlist-original"
        )
        self.assertEqual(
            self.settings["youtube_playlist_id"], "playlist-changed"
        )

    def test_manual_upload_keeps_legacy_executor_when_auto_executor_is_available(self):
        video = self.root / "manual.mp4"
        upload = mock.Mock(return_value="manual-video-id")
        auto_executor = mock.Mock()
        job_id = self.manager.create_upload_job([str(video)], "Manual upload")
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            get_youtube_service=mock.Mock(),
            safe_local_video_path=mock.Mock(
                side_effect=lambda raw, _settings: Path(raw)
            ),
            upload_to_youtube=upload,
            auto_youtube_executor=auto_executor,
        )

        run_upload_job(job_id, self.manager, dependencies)

        upload.assert_called_once_with(
            video, self.settings, job_id=job_id, item_id=f"{job_id}-item-1"
        )
        auto_executor.assert_not_called()
        self.assertEqual(self.manager.get_job(job_id)["item_states"], ["completed"])

    def test_explicit_empty_job_playlist_overrides_global_default(self):
        video = self.root / "one.mp4"
        upload = mock.Mock(return_value="id-1")

        self.run_worker([video], upload=upload, playlist_id="")

        self.assertEqual(
            upload.call_args.args[1]["youtube_playlist_id"], ""
        )

    def test_mixed_batch_uses_each_frozen_item_playlist(self):
        paths = [self.root / "one.mp4", self.root / "two.mp4"]
        upload = mock.Mock(side_effect=["id-1", "id-2"])
        job_id = self.manager.create_upload_job(
            [str(path) for path in paths],
            "Upload",
            playlist_id="legacy-job-playlist",
            item_metadata=[
                {
                    "streamer": "streamer_a",
                    "youtube_playlist_id": "PLAYLIST_A",
                },
                {
                    "streamer": "streamer_b",
                    "youtube_playlist_id": "PLAYLIST_B",
                },
            ],
        )
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: {
                **self.settings,
                "youtube_playlist_id": "changed-global",
            },
            append_log=self.manager.append_job_log,
            get_youtube_service=mock.Mock(),
            safe_local_video_path=mock.Mock(
                side_effect=lambda raw, _settings: Path(raw)
            ),
            upload_to_youtube=upload,
        )

        run_upload_job(job_id, self.manager, dependencies)

        self.assertEqual(
            [
                call.args[1]["youtube_playlist_id"]
                for call in upload.call_args_list
            ],
            ["PLAYLIST_A", "PLAYLIST_B"],
        )
        self.assertIn(
            "YouTube Settings: enabled=True, privacy=private, playlist=per-item",
            self.manager.jobs[job_id]["log"],
        )

    def test_frozen_empty_item_playlist_overrides_job_and_global(self):
        video = self.root / "one.mp4"
        upload = mock.Mock(return_value="id-1")
        job_id = self.manager.create_upload_job(
            [str(video)],
            "Upload",
            playlist_id="legacy-job-playlist",
            item_metadata=[{"youtube_playlist_id": ""}],
        )
        dependencies = UploadWorkerDependencies(
            load_settings=lambda: dict(self.settings),
            append_log=self.manager.append_job_log,
            get_youtube_service=mock.Mock(),
            safe_local_video_path=mock.Mock(
                side_effect=lambda raw, _settings: Path(raw)
            ),
            upload_to_youtube=upload,
        )

        run_upload_job(job_id, self.manager, dependencies)

        self.assertEqual(
            upload.call_args.args[1]["youtube_playlist_id"], ""
        )

    def test_single_file_upload_success(self):
        video = self.root / "one.mp4"
        upload = mock.Mock(return_value="id-1")

        job_id, _ = self.run_worker([video], upload=upload)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 0)
        self.assertEqual(self.manager.jobs[job_id]["item_statuses"], ["fertig"])
        self.assertEqual(self.manager.jobs[job_id]["item_progress"], [100])
        upload.assert_called_once_with(
            video, self.settings, job_id=job_id, item_id="1-item-1"
        )

    def test_multi_file_upload_success(self):
        paths = [self.root / "one.mp4", self.root / "two.mp4"]
        upload = mock.Mock(side_effect=["id-1", "id-2"])

        job_id, dependencies = self.run_worker(paths, upload=upload)

        self.assertEqual(self.manager.jobs[job_id]["status"], "fertig")
        self.assertEqual(self.manager.jobs[job_id]["returncode"], 0)
        self.assertEqual(
            self.manager.jobs[job_id]["item_statuses"], ["fertig", "fertig"]
        )
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            upload.call_args_list,
            [
                mock.call(
                    paths[0], self.settings, job_id=job_id, item_id="1-item-1"
                ),
                mock.call(
                    paths[1], self.settings, job_id=job_id, item_id="1-item-2"
                ),
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
        self.assertEqual(
            self.manager.jobs[job_id]["item_statuses"], ["fehler", "fertig"]
        )
        self.assertEqual(self.manager.jobs[job_id]["item_errors"][0], "first failed")
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
        self.assertEqual(self.manager.jobs[job_id]["item_statuses"], ["fehler"])
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
        dashboard.JOB_MANAGER.resume_queue("download")
        dashboard.JOB_MANAGER.resume_queue("youtube_upload")

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

    def test_create_upload_job_freezes_the_global_playlist_default(self):
        settings = {
            "youtube_playlist_id": "playlist-default",
            "youtube_uploaded_files": [],
        }
        metadata = {
            "streamer": "Example",
            "date_de": "2026-08-22",
            "title": "VOD",
            "vod_id": "123",
            "size_bytes": 100,
            "size_gb": 0.0,
        }
        with mock.patch.object(
            dashboard, "load_settings", return_value=settings
        ), mock.patch.object(
            dashboard,
            "safe_local_video_path",
            side_effect=lambda raw, _settings: Path(raw),
        ), mock.patch.object(
            dashboard, "local_video_metadata_payload", return_value=metadata
        ), mock.patch.object(
            dashboard.JOB_MANAGER, "start_worker"
        ):
            job_id = dashboard.create_upload_job(["C:/media/default.mp4"])

        self.assertEqual(
            dashboard.jobs[job_id]["playlist_id"], "playlist-default"
        )
        self.assertEqual(
            dashboard.jobs[job_id]["item_metadata"][0][
                "youtube_playlist_id"
            ],
            "playlist-default",
        )

    def test_create_upload_job_canonicalizes_v_prefixed_vod_metadata(self):
        settings = {"youtube_uploaded_files": []}
        for index, (raw_vod_id, expected) in enumerate((
            ("v2854335025", "2854335025"),
            ("2854335025", "2854335025"),
            ("", ""),
            (None, ""),
            ("not-a-twitch-vod", ""),
        )):
            with self.subTest(raw_vod_id=raw_vod_id), mock.patch.object(
                dashboard, "load_settings", return_value=settings
            ), mock.patch.object(
                dashboard, "safe_local_video_path", side_effect=lambda raw, _settings: Path(raw)
            ), mock.patch.object(
                dashboard, "local_video_metadata_payload", return_value={
                    "streamer": "Example", "date_de": "2026-08-22", "title": "VOD",
                    "vod_id": raw_vod_id, "size_bytes": 100, "size_gb": 0.0,
                }
            ), mock.patch.object(dashboard.JOB_MANAGER, "start_worker"):
                job_id = dashboard.create_upload_job([f"C:/media/vod-{index}.mp4"])
            self.assertEqual(dashboard.jobs[job_id]["item_metadata"][0]["vod_id"], expected)

    def test_create_upload_job_freezes_mixed_streamer_playlists_per_item(self):
        configured = {
            "youtube_playlist_id": "GLOBAL",
            "streamer_profiles": {
                "digitalgirluli": {"youtube_playlist_id": "PLAYLIST_A"},
                "nika_livetv": {"youtube_playlist_id": "PLAYLIST_B"},
            },
            "youtube_uploaded_files": [],
        }
        metadata = [
            {
                "streamer": "DigitalGirlUli",
                "date_de": "2026-08-22",
                "title": "First VOD",
                "vod_id": "123",
                "size_bytes": 100,
                "size_gb": 0.0,
            },
            {
                "streamer": "nika_livetv",
                "date_de": "2026-08-22",
                "title": "Second VOD",
                "vod_id": "456",
                "size_bytes": 200,
                "size_gb": 0.0,
            },
        ]
        with mock.patch.object(
            dashboard, "load_settings", return_value=configured
        ), mock.patch.object(
            dashboard,
            "safe_local_video_path",
            side_effect=lambda raw, _settings: Path(raw),
        ), mock.patch.object(
            dashboard,
            "local_video_metadata_payload",
            side_effect=metadata,
        ), mock.patch.object(
            dashboard.JOB_MANAGER, "start_worker"
        ):
            job_id = dashboard.create_upload_job(
                ["C:/media/first.mp4", "C:/media/second.mp4"]
            )

        job = dashboard.jobs[job_id]
        self.assertEqual(job["playlist_id"], "GLOBAL")
        self.assertEqual(
            [
                item["youtube_playlist_id"]
                for item in job["item_metadata"]
            ],
            ["PLAYLIST_A", "PLAYLIST_B"],
        )

        configured["youtube_playlist_id"] = "CHANGED_GLOBAL"
        configured["streamer_profiles"]["digitalgirluli"][
            "youtube_playlist_id"
        ] = "CHANGED_A"
        self.assertEqual(
            [
                item["youtube_playlist_id"]
                for item in job["item_metadata"]
            ],
            ["PLAYLIST_A", "PLAYLIST_B"],
        )

    def test_live_recording_upload_freezes_streamer_profile_playlist(self):
        configured = {
            "youtube_playlist_id": "GLOBAL",
            "streamer_profiles": {
                "nika_livetv": {"youtube_playlist_id": "NIKA_PLAYLIST"}
            },
            "youtube_uploaded_files": [],
        }
        metadata = {
            "streamer": "nika_livetv",
            "date_de": "23.08.2026",
            "title": "The actual broadcast title",
            "vod_id": "",
            "size_bytes": 100,
            "size_gb": 0.0,
        }
        with mock.patch.object(
            dashboard, "load_settings", return_value=configured
        ), mock.patch.object(
            dashboard,
            "safe_local_video_path",
            side_effect=lambda raw, _settings: Path(raw),
        ), mock.patch.object(
            dashboard, "local_video_metadata_payload", return_value=metadata
        ), mock.patch.object(
            dashboard.JOB_MANAGER, "start_worker"
        ):
            job_id = dashboard.create_upload_job(
                ["C:/media/live-recording.mp4"]
            )

        item = dashboard.jobs[job_id]["item_metadata"][0]
        self.assertEqual(item["streamer"], "nika_livetv")
        self.assertEqual(item["vod_id"], "")
        self.assertEqual(item["youtube_playlist_id"], "NIKA_PLAYLIST")

    def test_create_upload_job_freezes_explicit_no_playlist_per_item(self):
        configured = {
            "youtube_playlist_id": "GLOBAL",
            "streamer_profiles": {
                "example": {"youtube_playlist_id": "STREAMER"}
            },
            "youtube_uploaded_files": [],
        }
        metadata = {
            "streamer": "Example",
            "date_de": "2026-08-22",
            "title": "VOD",
            "vod_id": "123",
            "size_bytes": 100,
            "size_gb": 0.0,
        }
        with mock.patch.object(
            dashboard, "load_settings", return_value=configured
        ), mock.patch.object(
            dashboard,
            "safe_local_video_path",
            side_effect=lambda raw, _settings: Path(raw),
        ), mock.patch.object(
            dashboard, "local_video_metadata_payload", return_value=metadata
        ), mock.patch.object(
            dashboard.JOB_MANAGER, "start_worker"
        ):
            job_id = dashboard.create_upload_job(
                ["C:/media/no-playlist.mp4"], playlist_id=""
            )

        job = dashboard.jobs[job_id]
        self.assertEqual(job["playlist_id"], "")
        self.assertEqual(
            job["item_metadata"][0]["youtube_playlist_id"], ""
        )

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
        response_payload = response.get_json()
        self.assertEqual(response_payload["jobs"], payload)
        self.assertEqual(response_payload["persistence"], "process-local")
        self.assertIn("download", response_payload["queue_controls"])
        snapshots.assert_called_once_with(reverse=True)

    def test_resolve_error_route_hides_actionable_error_without_claiming_success(self):
        job_id = dashboard.JOB_MANAGER.create_upload_job(
            ["C:/media/failed.mp4"], "Upload"
        )
        dashboard.JOB_MANAGER.set_upload_item_status(
            job_id, 1, "fehler", error="quota exceeded"
        )
        client = dashboard.app.test_client()
        csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]

        response = client.post(
            "/api/jobs/resolve-error",
            json={
                "job_id": job_id,
                "item_id": dashboard.JOB_MANAGER.jobs[job_id]["item_ids"][0],
            },
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        job = dashboard.JOB_MANAGER.get_job(job_id)
        self.assertEqual(job["item_statuses"], ["fehler"])
        self.assertEqual(job["item_errors"], ["quota exceeded"])
        self.assertEqual(job["item_resolved"], [True])

    def test_retry_failed_download_creates_one_fresh_attempt(self):
        job_id = dashboard.JOB_MANAGER.create_download_job(
            ["https://www.twitch.tv/videos/1234567890"], "Download"
        )
        dashboard.JOB_MANAGER.set_download_item_status(job_id, 1, "fehler")
        item_id = dashboard.jobs[job_id]["item_ids"][0]
        client = dashboard.app.test_client()
        csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        with mock.patch.object(
            dashboard, "create_job", return_value="retry-download-2"
        ) as create_retry:
            first = client.post(
                "/api/jobs/retry-item",
                json={"job_id": job_id, "item_id": item_id},
                headers=headers,
            )
            duplicate = client.post(
                "/api/jobs/retry-item",
                json={"job_id": job_id, "item_id": item_id},
                headers=headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()["fresh_attempt"])
        self.assertTrue(duplicate.get_json()["duplicate"])
        create_retry.assert_called_once_with(
            ["https://www.twitch.tv/videos/1234567890"],
            "Retry Twitch Download",
            retry_of={"job_id": job_id, "item_id": item_id},
        )
        self.assertEqual(dashboard.jobs[job_id]["item_states"], ["failed"])

    def test_retry_failed_upload_is_new_session_only_when_outcome_known(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory)
            video = media_root / "failed.mp4"
            video.write_bytes(b"video")
            job_id = dashboard.JOB_MANAGER.create_upload_job(
                [str(video)], "Upload"
            )
            dashboard.JOB_MANAGER.set_upload_item_status(
                job_id, 1, "fehler", error="quota rejected"
            )
            item_id = dashboard.jobs[job_id]["item_ids"][0]
            client = dashboard.app.test_client()
            csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]

            with mock.patch.object(
                dashboard, "MEDIA_ROOT", media_root
            ), mock.patch.object(
                dashboard, "load_settings", return_value={}
            ), mock.patch.object(
                dashboard,
                "create_upload_job",
                return_value="retry-upload-2",
            ) as create_retry:
                response = client.post(
                    "/api/jobs/retry-item",
                    json={"job_id": job_id, "item_id": item_id},
                    headers={"X-CSRF-Token": csrf_token},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["fresh_attempt"])
        create_retry.assert_called_once_with(
            [str(video.resolve())],
            "Retry YouTube Upload",
            retry_of={"job_id": job_id, "item_id": item_id},
        )

    def test_retry_uncertain_upload_requires_user_verification(self):
        job_id = dashboard.JOB_MANAGER.create_upload_job(
            ["C:/media/uncertain.mp4"], "Upload"
        )
        item_id = dashboard.jobs[job_id]["item_ids"][0]
        dashboard.JOB_MANAGER.finish_claimed_item(
            job_id, item_id, "failed", failure_kind="uncertain"
        )
        client = dashboard.app.test_client()
        csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]

        with mock.patch.object(dashboard, "create_upload_job") as create_retry:
            response = client.post(
                "/api/jobs/retry-item",
                json={"job_id": job_id, "item_id": item_id},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["outcome_uncertain"])
        self.assertIn("YouTube Studio", response.get_json()["error"])
        create_retry.assert_not_called()

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

    def test_upload_route_forwards_an_explicit_playlist(self):
        client = dashboard.app.test_client()
        csrf_token = client.get("/api/auth/status").get_json()["csrf_token"]

        with mock.patch.object(
            dashboard, "create_upload_job", return_value="upload-1"
        ) as create_upload:
            response = client.post(
                "/api/youtube/upload-local",
                json={
                    "paths": ["C:/media/one.mp4"],
                    "playlist_id": "  playlist-explicit  ",
                },
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"job_id": "upload-1"})
        create_upload.assert_called_once_with(
            ["C:/media/one.mp4"], playlist_id="playlist-explicit"
        )


if __name__ == "__main__":
    unittest.main()
