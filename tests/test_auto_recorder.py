import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard import auto_recorder


class AutoRecorderStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.temp_dir.name) / "data"
        self.path = self.dashboard_dir / "auto-recorder-state.json"
        self.now = datetime(2026, 8, 23, 13, 30, tzinfo=timezone.utc)
        self.logs = []
        self.store = auto_recorder.AutoRecorderStateStore.from_dashboard_dir(
            self.dashboard_dir,
            clock=lambda: self.now,
            log=self.logs.append,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_json(self, value):
        self.dashboard_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def session_payload(self, **updates):
        payload = {
            "stream_id": "ABC_123",
            "disposition": "pending",
            "reason": None,
            "attempts": 0,
            "retry_after": None,
            "job_id": None,
            "updated_at": "2026-08-23T13:30:00Z",
        }
        payload.update(updates)
        return payload

    def test_state_path_and_missing_file_use_empty_version_one_state(self):
        self.assertEqual(
            auto_recorder.auto_recorder_state_path(self.dashboard_dir),
            self.path,
        )
        self.assertEqual(self.store.path, self.path)
        self.assertEqual(
            self.store.load(), {"version": 1, "sessions": {}}
        )
        self.assertFalse(self.path.exists())

    def test_valid_state_round_trip_is_normalized_and_detached(self):
        created = self.store.set_pending("@Nika_LiveTV", "ABC_123")
        snapshot = self.store.snapshot()
        created["stream_id"] = "mutated"
        snapshot["sessions"]["nika_livetv"]["stream_id"] = "mutated"

        reloaded = auto_recorder.AutoRecorderStateStore(self.path).load()

        self.assertEqual(
            reloaded,
            {
                "version": 1,
                "sessions": {
                    "nika_livetv": self.session_payload()
                },
            },
        )

    def test_streamer_keys_are_canonical_and_case_variants_do_not_duplicate(self):
        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "Nika_LiveTV": self.session_payload(
                        stream_id="OLD",
                        updated_at="2026-08-23T13:00:00Z",
                    ),
                    "@NIKA_LIVETV": self.session_payload(
                        stream_id="NEW",
                        updated_at="2026-08-23T14:00:00Z",
                    ),
                },
            }
        )

        loaded = self.store.load()

        self.assertEqual(list(loaded["sessions"]), ["nika_livetv"])
        self.assertEqual(
            loaded["sessions"]["nika_livetv"]["stream_id"], "NEW"
        )

    def test_invalid_streamers_are_discarded_or_rejected(self):
        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "../unsafe": self.session_payload(),
                    "valid_name": self.session_payload(stream_id="VALID"),
                },
            }
        )

        self.assertEqual(
            list(self.store.load()["sessions"]), ["valid_name"]
        )
        for streamer in ("", "bad-name!", "../unsafe"):
            with self.subTest(streamer=streamer), self.assertRaisesRegex(
                auto_recorder.AutoRecorderStateValidationError,
                "invalid_streamer",
            ):
                self.store.set_pending(streamer, "ABC")

    def test_stream_id_is_required_bounded_and_url_safe(self):
        for stream_id in (
            "",
            "   ",
            "https://twitch.tv/live",
            "ABC/123",
            "ABC?token=secret",
            "x" * 129,
            123,
        ):
            with self.subTest(stream_id=stream_id), self.assertRaisesRegex(
                auto_recorder.AutoRecorderStateValidationError,
                "invalid_stream_id",
            ):
                self.store.set_pending("nika_livetv", stream_id)

        session = self.store.set_pending("nika_livetv", "ABC-123_test")
        self.assertEqual(session["stream_id"], "ABC-123_test")

    def test_malformed_stream_id_and_disposition_do_not_load(self):
        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "empty_id": self.session_payload(stream_id=""),
                    "url_id": self.session_payload(
                        stream_id="https://signed.invalid/live"
                    ),
                    "bad_state": self.session_payload(disposition="live"),
                    "valid_name": self.session_payload(stream_id="GOOD"),
                },
            }
        )

        self.assertEqual(
            self.store.load()["sessions"],
            {"valid_name": self.session_payload(stream_id="GOOD")},
        )

    def test_pending_recording_and_handled_mutations_use_exact_schema(self):
        pending = self.store.set_pending("nika_livetv", "ABC", attempts=0)
        recording = self.store.set_recording(
            "nika_livetv", "ABC", job_id="recording-7", attempts=2
        )
        handled = self.store.set_handled(
            "nika_livetv", "ABC", "natural_end"
        )

        self.assertEqual(pending["disposition"], "pending")
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual(recording["disposition"], "recording")
        self.assertEqual(recording["attempts"], 2)
        self.assertEqual(recording["job_id"], "recording-7")
        self.assertEqual(handled["disposition"], "handled")
        self.assertEqual(handled["reason"], "natural_end")
        self.assertEqual(handled["attempts"], 2)
        self.assertEqual(handled["job_id"], "recording-7")
        self.assertEqual(
            set(handled),
            {
                "stream_id",
                "disposition",
                "reason",
                "attempts",
                "retry_after",
                "job_id",
                "updated_at",
            },
        )

    def test_attempts_accept_zero_and_positive_but_reject_unsafe_values(self):
        self.assertEqual(
            self.store.set_pending("zero", "STREAM", attempts=0)["attempts"],
            0,
        )
        self.assertEqual(
            self.store.set_pending("positive", "STREAM", attempts=9)[
                "attempts"
            ],
            9,
        )
        for value in (True, False, -1, 1001, "2", 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(
                auto_recorder.AutoRecorderStateValidationError,
                "invalid_attempts",
            ):
                self.store.set_pending("invalid", "STREAM", attempts=value)

    def test_invalid_persisted_attempts_discard_only_their_sessions(self):
        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "boolean": self.session_payload(attempts=True),
                    "negative": self.session_payload(attempts=-1),
                    "huge": self.session_payload(attempts=1001),
                    "valid": self.session_payload(attempts=3),
                },
            }
        )

        self.assertEqual(
            self.store.load()["sessions"],
            {"valid": self.session_payload(attempts=3)},
        )

    def test_retry_after_round_trips_in_utc_and_invalid_values_are_safe(self):
        session = self.store.set_pending(
            "nika_livetv",
            "ABC",
            retry_after="2026-08-23T15:30:00+02:00",
        )
        self.assertEqual(session["retry_after"], "2026-08-23T13:30:00Z")

        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "nika_livetv": self.session_payload(
                        retry_after="tomorrow"
                    )
                },
            }
        )
        self.assertIsNone(
            self.store.load()["sessions"]["nika_livetv"]["retry_after"]
        )
        with self.assertRaisesRegex(
            auto_recorder.AutoRecorderStateValidationError,
            "invalid_retry_after",
        ):
            self.store.set_pending(
                "nika_livetv", "ABC", retry_after="tomorrow"
            )

    def test_optional_job_id_round_trips_and_rejects_paths_or_urls(self):
        without_job = self.store.set_recording("first", "ABC")
        with_job = self.store.set_recording(
            "second", "XYZ", job_id="recording-42"
        )

        self.assertIsNone(without_job["job_id"])
        self.assertEqual(with_job["job_id"], "recording-42")
        for job_id in ("C:/media/video.mp4", "https://example.invalid/7"):
            with self.subTest(job_id=job_id), self.assertRaisesRegex(
                auto_recorder.AutoRecorderStateValidationError,
                "invalid_job_id",
            ):
                self.store.set_recording("third", "NEW", job_id=job_id)

    def test_updated_at_uses_injected_utc_clock(self):
        session = self.store.set_pending("nika_livetv", "ABC")
        self.assertEqual(session["updated_at"], "2026-08-23T13:30:00Z")

    def test_unknown_and_sensitive_persisted_fields_are_discarded(self):
        payload = self.session_payload()
        payload.update(
            {
                "manifest_url": "https://signed.invalid/?token=SECRET",
                "headers": {"Authorization": "SECRET"},
                "local_path": "C:/media/video.mp4",
                "exception": "SECRET failure",
            }
        )
        self.write_json(
            {"version": 1, "sessions": {"nika_livetv": payload}}
        )

        serialized = json.dumps(self.store.load())

        for secret in (
            "signed.invalid",
            "Authorization",
            "C:/media",
            "SECRET",
        ):
            self.assertNotIn(secret, serialized)

    def test_malformed_session_does_not_poison_valid_sibling(self):
        self.write_json(
            {
                "version": 1,
                "sessions": {
                    "broken": ["not", "a", "mapping"],
                    "valid": self.session_payload(stream_id="GOOD"),
                },
            }
        )
        self.assertEqual(
            self.store.load()["sessions"],
            {"valid": self.session_payload(stream_id="GOOD")},
        )

    def test_invalid_json_is_an_explicit_unhealthy_load(self):
        self.dashboard_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            '{"secret":"DO-NOT-LOG", broken', encoding="utf-8"
        )

        with self.assertRaises(
            auto_recorder.AutoRecorderStateLoadError
        ) as raised:
            self.store.load()

        self.assertEqual(raised.exception.reason, "invalid_json")
        self.assertNotIn("DO-NOT-LOG", "\n".join(self.logs))

    def test_wrong_top_level_values_are_explicitly_unhealthy(self):
        for value in ([], "text", 7, None):
            with self.subTest(value=value):
                self.write_json(value)
                with self.assertRaises(
                    auto_recorder.AutoRecorderStateLoadError
                ) as raised:
                    self.store.load()
                self.assertEqual(raised.exception.reason, "invalid_structure")

    def test_missing_unsupported_or_malformed_schema_is_fail_closed(self):
        cases = (
            ({"sessions": {}}, "invalid_structure"),
            ({"version": 2, "sessions": {}}, "unsupported_version"),
            ({"version": True, "sessions": {}}, "unsupported_version"),
            ({"version": 1, "sessions": []}, "invalid_structure"),
        )

        for value, expected_reason in cases:
            with self.subTest(value=value, expected_reason=expected_reason):
                self.write_json(value)
                with self.assertRaises(
                    auto_recorder.AutoRecorderStateLoadError
                ) as raised:
                    self.store.load()
                self.assertEqual(raised.exception.reason, expected_reason)

    def test_existing_unreadable_file_is_an_explicit_unhealthy_load(self):
        self.write_json({"version": 1, "sessions": {}})

        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("simulated")
        ), self.assertRaises(
            auto_recorder.AutoRecorderStateLoadError
        ) as raised:
            self.store.load()

        self.assertEqual(raised.exception.reason, "unreadable_state")
        self.assertTrue(self.path.exists())

    def test_unhealthy_state_cannot_be_overwritten_by_mutations(self):
        damaged = b'{"secret":"KEEP-FOR-RECOVERY", broken'
        mutations = (
            lambda: self.store.set_pending("nika_livetv", "ABC"),
            lambda: self.store.set_recording(
                "nika_livetv", "ABC", job_id="job-1"
            ),
            lambda: self.store.set_handled(
                "nika_livetv", "ABC", "manual_stop"
            ),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.dashboard_dir.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(damaged)
                with self.assertRaises(
                    auto_recorder.AutoRecorderStateLoadError
                ) as raised:
                    mutate()
                self.assertEqual(raised.exception.reason, "invalid_json")
                self.assertEqual(self.path.read_bytes(), damaged)

    def test_atomic_save_leaves_valid_json_and_no_temporary_file(self):
        self.store.set_pending("nika_livetv", "ABC")

        persisted = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(persisted, self.store.load())
        self.assertEqual(list(self.dashboard_dir.glob("*.tmp")), [])
        self.assertEqual(list(self.dashboard_dir.glob(".*.tmp")), [])

    def test_write_failure_preserves_previous_valid_file(self):
        self.store.set_pending("nika_livetv", "ORIGINAL")
        original = self.path.read_bytes()

        with mock.patch.object(
            auto_recorder.os, "replace", side_effect=OSError("simulated")
        ), self.assertRaises(auto_recorder.AutoRecorderStatePersistenceError):
            self.store.set_pending("other_streamer", "NEW")

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(json.loads(original), self.store.load())
        self.assertEqual(list(self.dashboard_dir.glob("*.tmp")), [])
        self.assertEqual(list(self.dashboard_dir.glob(".*.tmp")), [])

    def test_same_stream_matches_and_new_stream_replaces_old_session(self):
        self.store.set_handled(
            "nika_livetv", "ABC", "natural_end", attempts=1
        )

        self.assertTrue(self.store.session_matches("@NIKA_LIVETV", "ABC"))
        self.assertFalse(self.store.session_matches("nika_livetv", "XYZ"))
        replacement = self.store.set_pending("NIKA_LIVETV", "XYZ")

        self.assertEqual(replacement["stream_id"], "XYZ")
        self.assertEqual(replacement["disposition"], "pending")
        self.assertEqual(len(self.store.load()["sessions"]), 1)

    def test_handled_same_stream_is_not_implicitly_reset(self):
        handled = self.store.set_handled(
            "nika_livetv", "ABC", "manual_stop", attempts=1
        )
        self.now = datetime(2026, 8, 23, 14, 30, tzinfo=timezone.utc)

        pending = self.store.set_pending("nika_livetv", "ABC")
        recording = self.store.set_recording(
            "nika_livetv", "ABC", job_id="recording-9", attempts=2
        )

        self.assertEqual(pending, handled)
        self.assertEqual(recording, handled)
        self.assertEqual(
            self.store.get_session("nika_livetv")["disposition"], "handled"
        )

    def test_retry_update_is_metadata_only_and_does_not_reset_handled(self):
        self.store.set_pending("nika_livetv", "ABC")
        updated = self.store.update_retry(
            "nika_livetv",
            "ABC",
            attempts=2,
            retry_after="2026-08-23T14:00:00Z",
        )
        self.assertEqual(updated["attempts"], 2)
        self.assertEqual(updated["retry_after"], "2026-08-23T14:00:00Z")

        handled = self.store.set_handled(
            "nika_livetv", "ABC", "retry_exhausted"
        )
        unchanged = self.store.update_retry(
            "nika_livetv",
            "ABC",
            attempts=3,
            retry_after=None,
        )
        self.assertEqual(unchanged, handled)

    def test_recording_reservation_can_return_to_pending(self):
        self.store.set_pending("nika_livetv", "ABC", attempts=1)
        self.store.set_recording(
            "nika_livetv", "ABC", job_id="job-1", attempts=2
        )

        pending = self.store.return_to_pending(
            "nika_livetv", "ABC", attempts=1
        )

        self.assertEqual(pending["disposition"], "pending")
        self.assertEqual(pending["attempts"], 1)
        self.assertIsNone(pending["job_id"])

    def test_restart_reconciliation_changes_only_recording_sessions(self):
        self.store.set_pending("pending_one", "PENDING")
        self.store.set_recording(
            "recording_one", "RECORDING", job_id="job-1", attempts=2
        )
        self.store.set_handled(
            "handled_one", "HANDLED", "natural_end", attempts=1
        )
        before = self.store.load()
        self.now = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)

        changed = self.store.mark_interrupted_recordings_handled()
        after = self.store.load()

        self.assertEqual(changed, 1)
        self.assertEqual(after["sessions"]["pending_one"], before["sessions"]["pending_one"])
        self.assertEqual(after["sessions"]["handled_one"], before["sessions"]["handled_one"])
        interrupted = after["sessions"]["recording_one"]
        self.assertEqual(interrupted["disposition"], "handled")
        self.assertEqual(interrupted["reason"], "restart_interrupted")
        self.assertEqual(interrupted["attempts"], 2)
        self.assertEqual(interrupted["job_id"], "job-1")
        self.assertEqual(interrupted["updated_at"], "2026-08-23T15:00:00Z")

    def test_normal_mutation_api_rejects_raw_or_sensitive_metadata(self):
        with self.assertRaises(TypeError):
            self.store.set_pending(
                "nika_livetv",
                "ABC",
                manifest_url="https://signed.invalid/SECRET",
            )
        with self.assertRaises(auto_recorder.AutoRecorderStateValidationError):
            self.store.set_handled(
                "nika_livetv", "ABC", "exception token=SECRET"
            )
        self.assertFalse(self.path.exists())

    def test_thread_safe_mutations_preserve_all_streamer_sessions(self):
        failures = []

        def mutate(index):
            try:
                self.store.set_pending(
                    f"streamer_{index}", f"STREAM_{index}", attempts=index
                )
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=mutate, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        state = self.store.load()
        self.assertEqual(failures, [])
        self.assertEqual(len(state["sessions"]), 20)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), state)


if __name__ == "__main__":
    unittest.main()
