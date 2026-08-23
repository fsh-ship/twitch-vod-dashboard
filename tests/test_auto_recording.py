import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard.auto_recorder import AutoRecorderStateStore
from vod_dashboard.auto_recording import AutoRecorderCoordinator


class RecordingStarterError(RuntimeError):
    def __init__(self, reason, message=""):
        super().__init__(message or reason)
        self.reason = reason


class AutoRecorderCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "auto-recorder-state.json"
        self.store = AutoRecorderStateStore(
            self.path,
            clock=lambda: datetime(
                2026, 8, 23, 13, 30, tzinfo=timezone.utc
            ),
        )
        self.status_calls = []
        self.start_calls = []

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def settings(*streamers, enabled=True, extra_profiles=None):
        profiles = {
            streamer.lower(): {"auto_record": True}
            for streamer in streamers
        }
        profiles.update(extra_profiles or {})
        return {
            "auto_recorder_enabled": enabled,
            "streamer_profiles": profiles,
        }

    def coordinator(
        self,
        *,
        settings,
        streamers,
        statuses=None,
        checker=None,
        starter=None,
        store=None,
    ):
        statuses = statuses or {}

        def default_checker(streamer, current_settings):
            del current_settings
            self.status_calls.append(streamer)
            value = statuses[streamer]
            if isinstance(value, Exception):
                raise value
            return value

        def default_starter(streamer, **kwargs):
            self.start_calls.append((streamer, kwargs))
            return "job-1"

        return AutoRecorderCoordinator(
            settings_provider=lambda: settings,
            streamer_provider=lambda current_settings: list(streamers),
            live_status_checker=checker or default_checker,
            state_store=store or self.store,
            recording_starter=starter or default_starter,
        )

    def test_global_disabled_performs_no_state_check_status_or_start(self):
        self.path.write_text("{broken", encoding="utf-8")
        checker = mock.Mock()
        starter = mock.Mock()
        coordinator = self.coordinator(
            settings=self.settings("nika", enabled=False),
            streamers=["nika"],
            checker=checker,
            starter=starter,
        )

        result = coordinator.run_once()

        self.assertEqual(result["action"], "disabled")
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["state_healthy"])
        checker.assert_not_called()
        starter.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_enabled_without_watched_streamers_is_idle_without_io(self):
        self.path.write_text("{broken", encoding="utf-8")
        settings = self.settings(
            enabled=True,
            extra_profiles={
                "configured": {"auto_record": False},
                "profile_only": {"auto_record": True},
            },
        )
        checker = mock.Mock()
        starter = mock.Mock()

        result = self.coordinator(
            settings=settings,
            streamers=["configured"],
            checker=checker,
            starter=starter,
        ).run_once()

        self.assertEqual(result["action"], "idle")
        self.assertEqual(result["watched_count"], 0)
        self.assertEqual(result["checked_count"], 0)
        self.assertIsNone(result["state_healthy"])
        checker.assert_not_called()
        starter.assert_not_called()

    def test_only_configured_enabled_profiles_are_watched_in_file_order(self):
        settings = self.settings(
            "A",
            "C",
            extra_profiles={
                "b": {"auto_record": False},
                "profile_only": {"auto_record": True},
            },
        )
        statuses = {
            "a": {"state": "offline"},
            "c": {"state": "offline"},
        }

        result = self.coordinator(
            settings=settings,
            streamers=["@A", "B", "A", "C"],
            statuses=statuses,
        ).run_once()

        self.assertEqual(result["watched_count"], 2)
        self.assertEqual(result["checked_count"], 2)
        self.assertEqual(
            [outcome["streamer"] for outcome in result["outcomes"]],
            ["a", "c"],
        )
        self.assertEqual(set(self.status_calls), {"a", "c"})
        self.assertEqual(self.start_calls, [])

    def test_unhealthy_state_fails_closed_before_status_checks(self):
        self.path.write_text(
            json.dumps({"version": 2, "sessions": {}}), encoding="utf-8"
        )
        checker = mock.Mock()
        starter = mock.Mock()

        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            checker=checker,
            starter=starter,
        ).run_once()

        self.assertEqual(result["action"], "state_unhealthy")
        self.assertFalse(result["state_healthy"])
        self.assertEqual(result["reason"], "unsupported_version")
        self.assertEqual(result["checked_count"], 0)
        checker.assert_not_called()
        starter.assert_not_called()

    def test_confirmed_offline_preserves_previous_session(self):
        previous = self.store.set_handled(
            "nika", "OLD", "natural_end", attempts=1
        )

        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            statuses={"nika": {"state": "offline"}},
        ).run_once()

        self.assertEqual(result["offline_count"], 1)
        self.assertEqual(result["action"], "idle")
        self.assertEqual(self.store.get_session("nika"), previous)
        self.assertEqual(self.start_calls, [])

    def test_status_error_is_not_offline_and_siblings_continue(self):
        statuses = {
            "a": RuntimeError("https://signed.invalid/?token=SECRET"),
            "b": {"state": "offline"},
        }

        result = self.coordinator(
            settings=self.settings("a", "b"),
            streamers=["a", "b"],
            statuses=statuses,
        ).run_once()

        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["offline_count"], 1)
        self.assertEqual(result["checked_count"], 2)
        self.assertEqual(result["outcomes"][0]["status"], "error")
        self.assertEqual(result["outcomes"][1]["status"], "offline")
        self.assertNotIn("a", self.store.load()["sessions"])

    def test_live_without_stream_id_is_not_startable_or_persisted(self):
        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            statuses={"nika": {"state": "live", "title": "Live"}},
        ).run_once()

        self.assertEqual(result["live_count"], 1)
        self.assertEqual(
            result["outcomes"][0]["decision"], "missing_stream_id"
        )
        self.assertEqual(self.store.load()["sessions"], {})
        self.assertEqual(self.start_calls, [])

    def test_new_stream_replaces_old_and_conflict_leaves_it_pending(self):
        self.store.set_handled("nika", "OLD", "natural_end", attempts=1)

        def conflict(streamer, **kwargs):
            self.start_calls.append((streamer, kwargs))
            raise RecordingStarterError("recording_conflict")

        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            statuses={"nika": {"state": "live", "stream_id": "NEW"}},
            starter=conflict,
        ).run_once()

        session = self.store.get_session("nika")
        self.assertEqual(result["action"], "recording_conflict")
        self.assertEqual(session["stream_id"], "NEW")
        self.assertEqual(session["disposition"], "pending")
        self.assertEqual(session["attempts"], 0)

    def test_same_handled_or_recording_session_never_starts(self):
        cases = (("handled", "already_handled"), ("recording", "already_recording"))
        for disposition, expected in cases:
            with self.subTest(disposition=disposition):
                path = Path(self.temp_dir.name) / f"{disposition}.json"
                store = AutoRecorderStateStore(path)
                if disposition == "handled":
                    store.set_handled("nika", "ABC", "natural_end")
                else:
                    store.set_recording("nika", "ABC", job_id="job-old")
                starter = mock.Mock()

                result = self.coordinator(
                    settings=self.settings("nika"),
                    streamers=["nika"],
                    statuses={"nika": {"state": "live", "stream_id": "ABC"}},
                    starter=starter,
                    store=store,
                ).run_once()

                self.assertEqual(result["outcomes"][0]["decision"], expected)
                self.assertEqual(result["action"], "idle")
                starter.assert_not_called()

    def test_same_pending_session_is_eligible_and_next_attempt_is_used(self):
        self.store.set_pending("nika", "ABC", attempts=1)

        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            statuses={"nika": {"state": "live", "stream_id": "ABC"}},
        ).run_once()

        self.assertEqual(result["action"], "recording_started")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.start_calls[0][1]["attempt"], 2)
        self.assertEqual(self.store.get_session("nika")["attempts"], 2)

    def test_multiple_live_sessions_are_persisted_before_one_priority_start(self):
        statuses = {
            "a": {"state": "live", "stream_id": "A1"},
            "b": {"state": "live", "stream_id": "B1"},
            "c": {"state": "live", "stream_id": "C1"},
        }

        result = self.coordinator(
            settings=self.settings("a", "b", "c"),
            streamers=["a", "b", "c"],
            statuses=statuses,
        ).run_once()

        sessions = self.store.load()["sessions"]
        self.assertEqual(result["action"], "recording_started")
        self.assertEqual(result["streamer"], "a")
        self.assertEqual(len(self.start_calls), 1)
        self.assertEqual(sessions["a"]["disposition"], "recording")
        self.assertEqual(sessions["b"]["disposition"], "pending")
        self.assertEqual(sessions["c"]["disposition"], "pending")

    def test_configured_priority_wins_when_lower_priority_finishes_first(self):
        lower_finished = threading.Event()

        def checker(streamer, settings):
            del settings
            if streamer == "a":
                self.assertTrue(lower_finished.wait(timeout=2))
                return {"state": "live", "stream_id": "A1"}
            lower_finished.set()
            return {"state": "live", "stream_id": "B1"}

        result = self.coordinator(
            settings=self.settings("a", "b"),
            streamers=["a", "b"],
            checker=checker,
        ).run_once()

        self.assertEqual(result["streamer"], "a")
        self.assertEqual(self.start_calls[0][0], "a")

    def test_status_checks_never_exceed_two_concurrent_workers(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def checker(streamer, settings):
            nonlocal active, maximum
            del streamer, settings
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"state": "offline"}

        streamers = [f"streamer_{index}" for index in range(6)]
        result = self.coordinator(
            settings=self.settings(*streamers),
            streamers=streamers,
            checker=checker,
        ).run_once()

        self.assertEqual(result["checked_count"], 6)
        self.assertEqual(maximum, 2)

    def test_reservation_precedes_safe_auto_start_and_job_is_persisted(self):
        observed_reservation = {}

        def starter(streamer, **kwargs):
            observed_reservation.update(self.store.get_session(streamer))
            self.start_calls.append((streamer, kwargs))
            return "job-77"

        result = self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            statuses={
                "nika": {
                    "state": "live",
                    "stream_id": "ABC",
                    "title": "Safe title",
                    "started_at": "2026-08-23T12:00:00Z",
                    "manifest_url": "https://signed.invalid/?token=SECRET",
                }
            },
            starter=starter,
        ).run_once()

        call = self.start_calls[0]
        metadata = call[1]["live_metadata"]
        final = self.store.get_session("nika")
        self.assertEqual(observed_reservation["disposition"], "recording")
        self.assertEqual(observed_reservation["attempts"], 1)
        self.assertIsNone(observed_reservation["job_id"])
        self.assertEqual(call[1]["origin"], "auto")
        self.assertEqual(call[1]["attempt"], 1)
        self.assertEqual(metadata["stream_id"], "ABC")
        self.assertNotIn("manifest_url", metadata)
        self.assertEqual(result["action"], "recording_started")
        self.assertEqual(result["job_id"], "job-77")
        self.assertEqual(final["job_id"], "job-77")
        self.assertEqual(final["attempts"], 1)

    def test_conflict_does_not_consume_attempt_or_try_lower_priority(self):
        self.store.set_pending("a", "A1", attempts=1)

        def conflict(streamer, **kwargs):
            self.start_calls.append((streamer, kwargs))
            raise RecordingStarterError("recording_conflict")

        result = self.coordinator(
            settings=self.settings("a", "b"),
            streamers=["a", "b"],
            statuses={
                "a": {"state": "live", "stream_id": "A1"},
                "b": {"state": "live", "stream_id": "B1"},
            },
            starter=conflict,
        ).run_once()

        sessions = self.store.load()["sessions"]
        self.assertEqual(result["action"], "recording_conflict")
        self.assertEqual(len(self.start_calls), 1)
        self.assertEqual(self.start_calls[0][0], "a")
        self.assertEqual(sessions["a"]["disposition"], "pending")
        self.assertEqual(sessions["a"]["attempts"], 1)
        self.assertEqual(sessions["b"]["disposition"], "pending")

    def test_non_conflict_failure_consumes_attempt_and_stops_iteration(self):
        def fail(streamer, **kwargs):
            self.start_calls.append((streamer, kwargs))
            raise RecordingStarterError(
                "recording_start_failed",
                "https://signed.invalid/?token=SECRET C:/private/path",
            )

        result = self.coordinator(
            settings=self.settings("a", "b"),
            streamers=["a", "b"],
            statuses={
                "a": {"state": "live", "stream_id": "A1"},
                "b": {"state": "live", "stream_id": "B1"},
            },
            starter=fail,
        ).run_once()

        sessions = self.store.load()["sessions"]
        self.assertEqual(result["action"], "start_failed")
        self.assertEqual(result["reason"], "recording_start_failed")
        self.assertEqual(len(self.start_calls), 1)
        self.assertEqual(sessions["a"]["disposition"], "pending")
        self.assertEqual(sessions["a"]["attempts"], 1)
        self.assertEqual(sessions["b"]["disposition"], "pending")
        serialized = json.dumps(result)
        for secret in ("signed.invalid", "SECRET", "C:/private"):
            self.assertNotIn(secret, serialized)

    def test_status_result_and_starter_receive_one_safe_lookup_result(self):
        checks = 0
        received = []

        def checker(streamer, settings):
            nonlocal checks
            del settings
            checks += 1
            return {
                "state": "live",
                "streamer": streamer,
                "stream_id": "ABC",
                "title": "Live",
            }

        def starter(streamer, **kwargs):
            received.append((streamer, kwargs))
            return "job-1"

        self.coordinator(
            settings=self.settings("nika"),
            streamers=["nika"],
            checker=checker,
            starter=starter,
        ).run_once()

        self.assertEqual(checks, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["live_metadata"]["stream_id"], "ABC")
        self.assertEqual(received[0][1]["origin"], "auto")


if __name__ == "__main__":
    unittest.main()
