import json
import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard import auto_vod


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 24, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class AutoVodStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.temp_dir.name) / "dashboard"
        self.path = self.dashboard_dir / auto_vod.AUTO_VOD_STATE_FILE_NAME
        self.clock = AdvancingClock()
        self.store = auto_vod.AutoVodStateStore(self.path, clock=self.clock)
        self.streamer = "Nika_LiveTV"
        self.vod_id = "2854443252"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def pending(self):
        return self.store.ensure_pending(self.streamer, self.vod_id)

    def test_missing_file_is_healthy_empty_and_load_does_not_create_it(self):
        self.assertEqual(self.store.load(), auto_vod.empty_auto_vod_state())
        self.assertEqual(self.store.snapshot(), auto_vod.empty_auto_vod_state())
        self.assertFalse(self.path.exists())
        self.assertEqual(
            auto_vod.AutoVodStateStore.from_dashboard_dir(self.dashboard_dir).path,
            self.path,
        )

    def test_valid_roundtrip_uses_only_version_two_allowlisted_schema(self):
        pending = self.pending()
        queued = self.store.set_queued(self.streamer, self.vod_id, "17")
        handled = self.store.set_handled(
            self.streamer, self.vod_id, reason="downloaded"
        )

        self.assertEqual(pending["disposition"], "pending")
        self.assertEqual(queued["job_id"], "17")
        self.assertEqual(handled["disposition"], "handled")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, self.store.load())
        bucket = persisted["streamers"]["nika_livetv"]
        self.assertEqual(
            set(bucket),
            {"baseline_initialized", "baseline_established_at", "vods"},
        )
        self.assertFalse(bucket["baseline_initialized"])
        self.assertIsNone(bucket["baseline_established_at"])
        record = bucket["vods"][self.vod_id]
        self.assertEqual(
            set(record),
            {
                "disposition", "reason", "attempts", "retry_after", "job_id",
                "discovered_at", "updated_at",
            },
        )

    def test_invalid_or_corrupt_existing_state_fails_closed_and_is_preserved(self):
        cases = (
            ("{broken", "invalid_json"),
            (json.dumps([]), "invalid_structure"),
            (json.dumps({"version": 3, "streamers": {}}), "unsupported_version"),
            (json.dumps({"version": 1, "streamers": {"Nika": {"vods": {}}}}), "invalid_record"),
            (json.dumps({"version": 1, "streamers": {"nika": {"vods": {"2854443252": {}}}}}), "invalid_record"),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                self.dashboard_dir.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(raw.encode("utf-8"))
                with self.assertRaises(auto_vod.AutoVodStateLoadError) as raised:
                    self.store.load()
                self.assertEqual(raised.exception.reason, reason)
                with self.assertRaises(auto_vod.AutoVodStateLoadError):
                    self.pending()
                self.assertEqual(self.path.read_bytes(), raw.encode("utf-8"))

    def test_v1_state_requires_migration_and_is_not_overwritten(self):
        legacy = {
            "version": 1,
            "streamers": {
                "nika_livetv": {
                    "vods": {
                        self.vod_id: {
                            "disposition": "pending",
                            "reason": "new_vod",
                            "attempts": 0,
                            "retry_after": None,
                            "job_id": None,
                            "discovered_at": "2026-08-24T00:00:00Z",
                            "updated_at": "2026-08-24T00:00:00Z",
                        }
                    }
                }
            },
        }
        raw = json.dumps(legacy, indent=2).encode("utf-8")
        self.dashboard_dir.mkdir(parents=True)
        self.path.write_bytes(raw)

        with self.assertRaises(auto_vod.AutoVodStateMigrationRequired) as raised:
            self.store.load()
        self.assertEqual(raised.exception.reason, "migration_required")
        with self.assertRaises(auto_vod.AutoVodStateMigrationRequired):
            self.store.ensure_pending(self.streamer, "2854443251")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_unreadable_state_has_safe_reason(self):
        self.dashboard_dir.mkdir(parents=True)
        self.path.write_text("{}", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("secret")):
            with self.assertRaises(auto_vod.AutoVodStateLoadError) as raised:
                self.store.load()
        self.assertEqual(raised.exception.reason, "unreadable_state")

    def test_identity_is_canonical_and_rejects_unsafe_values(self):
        record = self.store.ensure_pending("@Nika_LiveTV", "v2854443252")
        self.assertEqual(record["disposition"], "pending")
        self.assertIn(
            "2854443252", self.store.load()["streamers"]["nika_livetv"]["vods"]
        )
        for streamer, vod_id, expected in (
            ("not-valid!", self.vod_id, "invalid_streamer"),
            (self.streamer, "https://www.twitch.tv/videos/2854443252", "invalid_vod_id"),
            (self.streamer, "../2854443252", "invalid_vod_id"),
            (self.streamer, "-2854443252", "invalid_vod_id"),
        ):
            with self.subTest(vod_id=vod_id):
                with self.assertRaises(auto_vod.AutoVodStateValidationError) as raised:
                    self.store.ensure_pending(streamer, vod_id)
                self.assertEqual(str(raised.exception), expected)

    def test_establish_baseline_is_atomic_idempotent_and_canonical(self):
        self.assertFalse(self.store.baseline_initialized(self.streamer))
        bucket = self.store.establish_baseline(
            "@Nika_LiveTV", ["v2854443252", "2854443251", "2854443252"]
        )

        self.assertTrue(bucket["baseline_initialized"])
        self.assertEqual(bucket["baseline_established_at"], "2026-08-24T00:00:00Z")
        self.assertTrue(self.store.baseline_initialized(self.streamer))
        self.assertEqual(set(bucket["vods"]), {"2854443252", "2854443251"})
        for record in bucket["vods"].values():
            self.assertEqual(record["disposition"], "handled")
            self.assertEqual(record["reason"], "baseline_existing")
            self.assertEqual(record["attempts"], 0)
            self.assertIsNone(record["retry_after"])
            self.assertIsNone(record["job_id"])

        original = self.path.read_bytes()
        repeated = self.store.establish_baseline(self.streamer, ["2854443000"])
        self.assertEqual(repeated, bucket)
        self.assertEqual(self.path.read_bytes(), original)

    def test_empty_establish_baseline_persists_initialized_marker(self):
        bucket = self.store.establish_baseline(self.streamer, [])
        self.assertTrue(bucket["baseline_initialized"])
        self.assertEqual(bucket["vods"], {})
        persisted = self.store.load()["streamers"]["nika_livetv"]
        self.assertTrue(persisted["baseline_initialized"])
        self.assertIsNotNone(persisted["baseline_established_at"])

    def test_baseline_preserves_existing_handled_history_reason(self):
        self.pending()
        self.store.set_handled(self.streamer, self.vod_id, reason="downloaded")

        bucket = self.store.establish_baseline(self.streamer, [self.vod_id])

        self.assertEqual(bucket["vods"][self.vod_id]["reason"], "downloaded")

    def test_failed_baseline_write_leaves_no_partial_marker_or_records(self):
        with mock.patch.object(auto_vod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(auto_vod.AutoVodStatePersistenceError):
                self.store.establish_baseline(self.streamer, [self.vod_id])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.store.baseline_initialized(self.streamer))

    def test_pending_queued_handled_transitions_are_explicit_and_sticky(self):
        first = self.pending()
        file_before = self.path.read_bytes()
        self.assertEqual(self.pending(), first)
        self.assertEqual(self.path.read_bytes(), file_before)

        queued = self.store.set_queued(self.streamer, self.vod_id, "9")
        self.assertEqual(queued["disposition"], "queued")
        self.assertEqual(self.store.set_queued(self.streamer, self.vod_id, "9"), queued)
        with self.assertRaisesRegex(auto_vod.AutoVodStateValidationError, "job_ownership_conflict"):
            self.store.set_queued(self.streamer, self.vod_id, "10")

        handled = self.store.set_handled(self.streamer, self.vod_id, reason="downloaded")
        self.assertEqual(handled["job_id"], "9")
        self.assertEqual(self.pending(), handled)
        self.assertEqual(
            self.store.set_handled(self.streamer, self.vod_id, reason="ignored"),
            handled,
        )

    def test_retry_returns_queued_ownership_to_pending_and_preserves_discovery_time(self):
        pending = self.pending()
        queued = self.store.set_queued(self.streamer, self.vod_id, "21")
        retried = self.store.update_retry(
            self.streamer,
            self.vod_id,
            attempts=2,
            retry_after="2026-08-24T12:00:00+02:00",
            reason="job_failed",
        )
        self.assertEqual(queued["discovered_at"], pending["discovered_at"])
        self.assertEqual(retried["discovered_at"], pending["discovered_at"])
        self.assertEqual(retried["disposition"], "pending")
        self.assertEqual(retried["reason"], "job_failed")
        self.assertEqual(retried["attempts"], 2)
        self.assertEqual(retried["retry_after"], "2026-08-24T10:00:00Z")
        self.assertIsNone(retried["job_id"])
        self.assertGreater(retried["updated_at"], queued["updated_at"])

    def test_transition_input_validation_is_strict(self):
        self.pending()
        invalid_calls = (
            lambda: self.store.set_pending(self.streamer, self.vod_id, reason="bad reason", attempts=0),
            lambda: self.store.set_pending(self.streamer, self.vod_id, reason="retry_wait", attempts=True),
            lambda: self.store.set_pending(self.streamer, self.vod_id, reason="retry_wait", attempts=-1),
            lambda: self.store.set_pending(self.streamer, self.vod_id, reason="retry_wait", attempts=0, retry_after="2026-08-24T12:00:00"),
            lambda: self.store.set_queued(self.streamer, self.vod_id, "uuid-123"),
        )
        for call in invalid_calls:
            with self.assertRaises(auto_vod.AutoVodStateValidationError):
                call()
        self.assertEqual(self.store.set_queued(self.streamer, self.vod_id, "22")["job_id"], "22")

    def test_retention_prunes_only_deterministic_non_baseline_handled_records(self):
        def record(disposition, updated_at, *, reason=None, job_id=None):
            return {
                "disposition": disposition,
                "reason": reason,
                "attempts": 0,
                "retry_after": None,
                "job_id": job_id,
                "discovered_at": "2026-08-24T00:00:00Z",
                "updated_at": updated_at,
            }

        vods = {
            str(2_854_443_000 + index): record(
                "handled", f"2026-08-24T00:{index // 60:02d}:{index % 60:02d}Z", reason="downloaded"
            )
            for index in range(501)
        }
        vods["2854999001"] = record("pending", "2026-08-24T00:00:00Z", reason="new_vod")
        vods["2854999002"] = record("queued", "2026-08-24T00:00:00Z", job_id="1")
        vods["2854999003"] = record("handled", "2026-08-24T00:00:00Z", reason="baseline_existing")
        state = {
            "version": 2,
            "streamers": {
                "nika_livetv": {
                    "baseline_initialized": True,
                    "baseline_established_at": "2026-08-24T00:00:00Z",
                    "vods": vods,
                }
            },
        }

        retained = auto_vod.apply_auto_vod_retention(state)
        retained_vods = retained["streamers"]["nika_livetv"]["vods"]
        self.assertEqual(sum(r["disposition"] == "handled" for r in retained_vods.values()), 501)
        self.assertNotIn("2854443000", retained_vods)
        self.assertIn("2854443500", retained_vods)
        self.assertIn("2854999001", retained_vods)
        self.assertIn("2854999002", retained_vods)
        self.assertIn("2854999003", retained_vods)
        self.assertEqual(retained, auto_vod.apply_auto_vod_retention(state))

    def test_atomic_write_failures_and_invalid_outgoing_changes_preserve_primary(self):
        self.pending()
        original = self.path.read_bytes()
        with mock.patch.object(auto_vod.os, "replace", side_effect=OSError("secret")):
            with self.assertRaises(auto_vod.AutoVodStatePersistenceError):
                self.store.ensure_pending(self.streamer, "2854443251")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.dashboard_dir.glob("*.tmp")), [])

        with self.assertRaises(auto_vod.AutoVodStateValidationError):
            self.store.set_pending(self.streamer, self.vod_id, reason="signed_url=secret", attempts=0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.store.load())

    def test_concurrent_mutations_remain_valid_json(self):
        failures = []

        def reserve(index):
            try:
                self.store.ensure_pending("nika_livetv", str(2_854_443_000 + index))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, self.store.load())
        self.assertEqual(len(persisted["streamers"]["nika_livetv"]["vods"]), 20)

    def test_untrusted_extra_fields_never_persist_and_store_has_no_runtime_dependencies(self):
        self.pending()
        persisted = self.path.read_text(encoding="utf-8")
        for sentinel in (
            "cookie", "token", "signed_url", "command", "title", "log",
            "exception", "media_path", "youtube_playlist_id",
        ):
            self.assertNotIn(sentinel, persisted)

        source = Path(auto_vod.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("jobmanager", "job_store", "archive.txt", "flask", "thread("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
