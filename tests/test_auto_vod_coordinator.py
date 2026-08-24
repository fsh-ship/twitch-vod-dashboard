import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard.auto_vod import AutoVodStateLoadError, AutoVodStateStore
from vod_dashboard.auto_vod_coordinator import AutoVodCoordinator
from vod_dashboard.auto_vod_storage import AutoVodStorageStatus


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class FakeManager:
    def __init__(self):
        self.jobs = {}
        self.created = []
        self.started = []
        self.fail_create = False

    def create_download_job(self, urls, label, **metadata):
        if self.fail_create:
            raise RuntimeError("persistence unavailable SECRET")
        job_id = str(len(self.jobs) + 1)
        job = {"id": job_id, "type": "download", "label": label, "urls": urls, "state": "queued", **metadata}
        self.jobs[job_id] = job
        self.created.append(job)
        return job_id

    def get_job(self, job_id):
        return self.jobs.get(str(job_id))

    def start_worker(self, target, job_id):
        self.started.append(str(job_id))


class AutoVodCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AutoVodStateStore(Path(self.temp.name) / "auto-vod-state.json", clock=lambda: NOW)
        self.manager = FakeManager()
        self.settings = {
            "auto_vod_enabled": True,
            "streamer_profiles": {"alpha": {"auto_vod_download": True}, "beta": {"auto_vod_download": True}},
            "youtube_auto_upload": True,
        }
        self.streamers = ["alpha", "beta", "ignored"]
        self.archive = set()
        self.discovery_calls = []

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def vod(vod_id):
        return {"twitch_vod_id": str(vod_id), "canonical_url": f"https://www.twitch.tv/videos/{vod_id}", "title": "VOD", "upload_date": None}

    def coordinator(
        self,
        discovery=None,
        state_store=None,
        worker_starter=None,
        storage_provider=None,
        should_stop=None,
    ):
        def discover(streamer, settings, *, limit):
            self.discovery_calls.append((streamer, limit, settings))
            return (discovery or (lambda value: {"vods": []}))(streamer)
        return AutoVodCoordinator(
            settings_provider=lambda: self.settings,
            streamer_provider=lambda settings: self.streamers,
            state_store=state_store or self.store,
            job_manager=self.manager,
            archive_ids_provider=lambda settings: self.archive,
            worker_target=lambda job_id: None,
            discovery=discover,
            clock=lambda: NOW,
            worker_starter=worker_starter,
            storage_provider=storage_provider
            or (lambda settings: AutoVodStorageStatus("sufficient", 100, 200, 50)),
            should_stop=should_stop,
        )

    def establish_baseline(self, streamer="alpha", vod_ids=()):
        return self.store.establish_baseline(streamer, vod_ids)

    def test_disabled_and_no_selected_streamers_have_zero_side_effects(self):
        self.settings["auto_vod_enabled"] = False
        result = self.coordinator().run_once()
        self.assertEqual(result["action"], "disabled")
        self.assertEqual(self.discovery_calls, [])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.manager.created, [])

        self.settings["auto_vod_enabled"] = True
        self.settings["streamer_profiles"] = {}
        result = self.coordinator().run_once()
        self.assertEqual(result["action"], "no_streamers")
        self.assertEqual(self.discovery_calls, [])

    def test_selected_streamers_preserve_configured_order_and_limit_ten(self):
        result = self.coordinator(lambda streamer: {"vods": [self.vod("2854443252")] if streamer == "beta" else []}).run_once()
        self.assertEqual([call[0] for call in self.discovery_calls], ["alpha", "beta"])
        self.assertTrue(all(call[1] == 10 for call in self.discovery_calls))
        self.assertEqual(result["watched_count"], 2)
        self.assertEqual(self.manager.created, [])
        self.assertTrue(self.store.baseline_initialized("alpha"))
        self.assertTrue(self.store.baseline_initialized("beta"))
        self.assertEqual(
            self.store.get_vod("beta", "2854443252")["reason"],
            "baseline_existing",
        )

    def test_discovery_is_bounded_to_two_workers_and_one_failure_does_not_stop_others(self):
        self.settings["streamer_profiles"]["gamma"] = {"auto_vod_download": True}
        self.streamers.append("gamma")
        active = 0
        maximum = 0
        lock = threading.Lock()

        def discovery(streamer):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            if streamer == "beta":
                raise RuntimeError("private discovery detail")
            return {"vods": [self.vod("2854443252" if streamer == "alpha" else "2854443251")]}

        result = self.coordinator(discovery).run_once()

        self.assertLessEqual(maximum, 2)
        self.assertGreaterEqual(maximum, 2)
        self.assertEqual(result["errors"], [{"streamer": "beta", "code": "yt_dlp_failed"}])
        self.assertEqual(self.manager.created, [])
        self.assertTrue(self.store.baseline_initialized("alpha"))
        self.assertFalse(self.store.baseline_initialized("beta"))
        self.assertTrue(self.store.baseline_initialized("gamma"))

    def test_unhealthy_state_fails_closed_before_archive_or_discovery(self):
        bad = mock.Mock()
        bad.snapshot.side_effect = AutoVodStateLoadError("invalid_json")
        archive = mock.Mock()
        coordinator = AutoVodCoordinator(
            settings_provider=lambda: self.settings, streamer_provider=lambda settings: self.streamers,
            state_store=bad, job_manager=self.manager, archive_ids_provider=archive,
            worker_target=lambda job_id: None, discovery=mock.Mock(), clock=lambda: NOW,
        )
        result = coordinator.run_once()
        self.assertEqual(result["action"], "state_unhealthy")
        archive.assert_not_called()
        self.assertEqual(self.manager.created, [])

    def test_archive_and_manual_jobs_prevent_duplicate_auto_jobs(self):
        self.establish_baseline("alpha", [])
        self.archive.add("2854443252")
        self.manager.jobs["7"] = {"id": "7", "type": "download", "origin": "manual", "urls": ["https://www.twitch.tv/videos/2854443251"], "state": "queued"}
        self.coordinator(lambda streamer: {"vods": [self.vod("2854443252"), self.vod("2854443251")]}).run_once()
        self.assertEqual(self.manager.created, [])
        self.assertEqual(
            self.store.get_vod("alpha", "2854443252")["reason"],
            "archive_present",
        )
        waiting = self.store.get_vod("alpha", "2854443251")
        self.assertEqual(waiting["disposition"], "pending")

    def test_crash_window_rebinds_matching_auto_job_and_completed_or_cancelled_jobs_are_handled(self):
        self.establish_baseline("alpha", [])
        self.store.ensure_pending("alpha", "2854443252")
        self.manager.jobs["3"] = {"id": "3", "type": "download", "origin": "auto_vod", "streamer": "alpha", "twitch_vod_id": "2854443252", "attempt": 1, "urls": ["https://www.twitch.tv/videos/2854443252"], "state": "queued"}
        self.coordinator().run_once()
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["job_id"], "3")
        self.assertEqual(self.manager.created, [])

        self.store.ensure_pending("alpha", "2854443251")
        self.store.set_queued("alpha", "2854443251", "4", attempts=1)
        self.manager.jobs["4"] = {"id": "4", "type": "download", "origin": "auto_vod", "streamer": "alpha", "twitch_vod_id": "2854443251", "attempt": 1, "urls": ["https://www.twitch.tv/videos/2854443251"], "state": "completed"}
        self.coordinator().run_once()
        self.assertEqual(self.store.get_vod("alpha", "2854443251")["reason"], "downloaded")

    def test_queued_state_never_binds_a_manual_job_with_the_same_url(self):
        self.establish_baseline("alpha", [])
        self.store.ensure_pending("alpha", "2854443252")
        self.store.set_queued("alpha", "2854443252", "9", attempts=1)
        self.manager.jobs["9"] = {
            "id": "9",
            "type": "download",
            "origin": "manual",
            "urls": ["https://www.twitch.tv/videos/2854443252"],
            "state": "completed",
        }

        result = self.coordinator().run_once()

        self.assertEqual(result["errors"], [{"streamer": "alpha", "code": "state_job_inconsistent"}])
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["disposition"], "queued")

    def test_failed_retry_cooldowns_and_exhaustion_are_durable(self):
        self.establish_baseline("alpha", [])
        for vod_id, attempts in (("2854443252", 1), ("2854443251", 2), ("2854443250", 3)):
            self.store.ensure_pending("alpha", vod_id)
            job_id = str(attempts + 10)
            self.store.set_queued("alpha", vod_id, job_id, attempts=attempts)
            self.manager.jobs[job_id] = {"id": job_id, "type": "download", "origin": "auto_vod", "streamer": "alpha", "twitch_vod_id": vod_id, "attempt": attempts, "urls": [f"https://www.twitch.tv/videos/{vod_id}"], "state": "failed"}
        self.coordinator().run_once()
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["retry_after"], "2026-08-24T13:00:00Z")
        self.assertEqual(self.store.get_vod("alpha", "2854443251")["retry_after"], "2026-08-24T14:00:00Z")
        self.assertEqual(self.store.get_vod("alpha", "2854443250")["reason"], "retry_exhausted")

    def test_crash_window_failed_auto_job_uses_the_normal_retry_cooldown(self):
        self.establish_baseline("alpha", [])
        self.store.ensure_pending("alpha", "2854443252")
        self.manager.jobs["3"] = {
            "id": "3", "type": "download", "origin": "auto_vod",
            "streamer": "alpha", "twitch_vod_id": "2854443252", "attempt": 1,
            "urls": ["https://www.twitch.tv/videos/2854443252"], "state": "failed",
        }

        self.coordinator().run_once()

        record = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(record["disposition"], "pending")
        self.assertEqual(record["retry_after"], "2026-08-24T13:00:00Z")
        self.assertEqual(self.manager.created, [])

    def test_due_pending_retry_without_rediscovery_schedules_once_and_attempts_match(self):
        self.establish_baseline("alpha", [])
        self.store.ensure_pending("alpha", "2854443252")
        self.store.set_pending("alpha", "2854443252", reason="job_failed", attempts=1, retry_after="2026-08-24T11:00:00Z")
        self.coordinator(lambda streamer: {"vods": []}).run_once()
        self.assertEqual(len(self.manager.created), 1)
        job = self.manager.created[0]
        self.assertEqual(job["attempt"], 2)
        state = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["job_id"], job["id"])

    def test_newest_first_discovery_schedules_oldest_first_with_bounded_limits(self):
        self.establish_baseline("alpha", [])
        self.establish_baseline("beta", [])
        def discoveries(streamer):
            if streamer == "alpha":
                return {"vods": [self.vod(2854443252 - index) for index in range(5)]}
            return {"vods": [self.vod(2854443240 - index) for index in range(5)]}
        self.coordinator(discoveries).run_once()
        self.assertEqual([job["streamer"] for job in self.manager.created], ["alpha"])
        self.assertEqual([job["twitch_vod_id"] for job in self.manager.created], ["2854443248"])

    def test_failed_manual_job_does_not_block_a_new_auto_attempt(self):
        self.establish_baseline("alpha", [])
        self.manager.jobs["7"] = {
            "id": "7", "type": "download", "origin": "manual",
            "urls": ["https://www.twitch.tv/videos/2854443252"], "state": "failed",
        }

        self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]} if streamer == "alpha" else {"vods": []}
        ).run_once()

        self.assertEqual(len(self.manager.created), 1)
        self.assertEqual(self.manager.created[0]["origin"], "auto_vod")

    def test_job_or_state_persistence_failure_never_starts_worker_or_consumes_attempt(self):
        self.establish_baseline("alpha", [])
        self.manager.fail_create = True
        result = self.coordinator(lambda streamer: {"vods": [self.vod("2854443252")]}).run_once()
        self.assertEqual(result["action"], "job_persistence_failed")
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["attempts"], 0)
        self.manager.fail_create = False

        class FailingBindStore(AutoVodStateStore):
            def set_queued(self, *args, **kwargs):
                raise AutoVodStateLoadError("unreadable_state")
        failing = FailingBindStore(Path(self.temp.name) / "other.json", clock=lambda: NOW)
        failing.establish_baseline("alpha", [])
        result = self.coordinator(lambda streamer: {"vods": [self.vod("2854443251")]}, state_store=failing).run_once()
        self.assertEqual(result["action"], "state_persistence_failed")
        self.assertEqual(self.manager.started, [])
        self.assertEqual(len(self.manager.created), 1)

    def test_worker_start_failure_leaves_the_durable_job_and_binding_for_reconciliation(self):
        self.establish_baseline("alpha", [])
        result = self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]},
            worker_starter=mock.Mock(side_effect=RuntimeError("start failed")),
        ).run_once()

        self.assertEqual(result["action"], "worker_start_failed")
        self.assertEqual(result["errors"], [{"streamer": "alpha", "code": "worker_start_failed"}])
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["job_id"], "1")
        self.assertEqual(self.manager.created[0]["attempt"], 1)

    def test_first_successful_discovery_establishes_baseline_and_later_id_queues_once(self):
        discoveries = [
            {"vods": [self.vod("2854443252"), self.vod("2854443251")]},
            {"vods": [self.vod("2854443253"), self.vod("2854443252")]},
            {"vods": [self.vod("2854443253"), self.vod("2854443252")]},
        ]

        def discovery(streamer):
            if streamer == "alpha":
                return discoveries.pop(0)
            return {"vods": []}

        self.coordinator(discovery).run_once()
        self.assertEqual(self.manager.created, [])
        self.assertTrue(self.store.baseline_initialized("alpha"))
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["reason"], "baseline_existing")

        self.coordinator(discovery).run_once()
        self.assertEqual([job["twitch_vod_id"] for job in self.manager.created], ["2854443253"])
        self.assertEqual(self.manager.created[0]["origin"], "auto_vod")
        self.assertEqual(self.manager.created[0]["post_download_mode"], "download_only")
        self.assertEqual(self.manager.created[0]["attempt"], 1)

        self.coordinator(discovery).run_once()
        self.assertEqual(len(self.manager.created), 1)

    def test_empty_first_discovery_baselines_and_failure_does_not(self):
        self.coordinator(lambda streamer: {"vods": []}).run_once()
        self.assertTrue(self.store.baseline_initialized("alpha"))
        self.assertEqual(self.store.load()["streamers"]["alpha"]["vods"], {})

        fresh = AutoVodStateStore(Path(self.temp.name) / "fresh.json", clock=lambda: NOW)
        result = self.coordinator(
            lambda streamer: {"vods": [], "error": {"code": "yt_dlp_failed"}},
            state_store=fresh,
        ).run_once()
        self.assertFalse(fresh.baseline_initialized("alpha"))
        self.assertEqual(result["error_count"], 2)

        self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]},
            state_store=fresh,
        ).run_once()
        self.assertTrue(fresh.baseline_initialized("alpha"))
        self.assertEqual(self.manager.created, [])

    def test_disable_reenable_keeps_baseline_and_known_id_is_never_pending(self):
        self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]} if streamer == "alpha" else {"vods": []}
        ).run_once()
        baseline = self.store.load()["streamers"]["alpha"]
        self.settings["auto_vod_enabled"] = False
        self.coordinator().run_once()
        self.assertEqual(self.store.load()["streamers"]["alpha"], baseline)

        self.settings["auto_vod_enabled"] = True
        self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]} if streamer == "alpha" else {"vods": []}
        ).run_once()
        self.assertEqual(self.manager.created, [])
        record = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(record["disposition"], "handled")
        self.assertEqual(record["reason"], "baseline_existing")

    def test_migration_required_creates_no_jobs_or_state_mutations(self):
        legacy_path = Path(self.temp.name) / "legacy.json"
        legacy = {
            "version": 1,
            "streamers": {
                "alpha": {
                    "vods": {
                        "2854443252": {
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
        raw = json.dumps(legacy).encode("utf-8")
        legacy_path.write_bytes(raw)
        legacy_store = AutoVodStateStore(legacy_path, clock=lambda: NOW)

        result = self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443253")]},
            state_store=legacy_store,
        ).run_once()

        self.assertEqual(result["action"], "migration_required")
        self.assertEqual(result["errors"], [{"code": "migration_required"}])
        self.assertEqual(self.discovery_calls, [])
        self.assertEqual(self.manager.created, [])
        self.assertEqual(legacy_path.read_bytes(), raw)

    def test_low_storage_discovers_and_persists_pending_without_attempt_or_retry(self):
        self.establish_baseline("alpha", [])
        low = lambda settings: AutoVodStorageStatus("insufficient", 1, 200, 50)

        result = self.coordinator(
            lambda streamer: {
                "vods": [self.vod("2854443252"), self.vod("2854443251")]
                if streamer == "alpha"
                else []
            },
            storage_provider=low,
        ).run_once()

        self.assertEqual(result["action"], "storage_insufficient")
        self.assertEqual(result["storage_state"], "insufficient")
        self.assertEqual(result["storage_blocked_count"], 2)
        self.assertEqual(self.manager.created, [])
        for vod_id in ("2854443251", "2854443252"):
            record = self.store.get_vod("alpha", vod_id)
            self.assertEqual(record["disposition"], "pending")
            self.assertEqual(record["reason"], "storage_blocked")
            self.assertEqual(record["attempts"], 0)
            self.assertIsNone(record["retry_after"])
            self.assertIsNone(record["job_id"])
        self.assertEqual([call[0] for call in self.discovery_calls], ["alpha", "beta"])

    def test_storage_recovery_schedules_the_same_pending_vod_once(self):
        self.establish_baseline("alpha", [])
        state = {"value": "insufficient"}

        def storage(settings):
            return AutoVodStorageStatus(state["value"], 100, 200, 50)

        discovery = lambda streamer: {
            "vods": [self.vod("2854443252")] if streamer == "alpha" else []
        }
        self.coordinator(discovery, storage_provider=storage).run_once()
        state["value"] = "sufficient"
        result = self.coordinator(discovery, storage_provider=storage).run_once()

        self.assertEqual(result["action"], "queued")
        self.assertEqual([job["twitch_vod_id"] for job in self.manager.created], ["2854443252"])
        record = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(record["disposition"], "queued")
        self.assertEqual(record["attempts"], 1)

    def test_unavailable_storage_fails_closed_without_consuming_attempt(self):
        self.establish_baseline("alpha", [])
        result = self.coordinator(
            lambda streamer: {
                "vods": [self.vod("2854443252")] if streamer == "alpha" else []
            },
            storage_provider=lambda settings: AutoVodStorageStatus(
                "unavailable", None, None, None
            ),
        ).run_once()

        self.assertEqual(result["action"], "storage_unavailable")
        self.assertEqual(result["storage_state"], "unavailable")
        self.assertEqual(self.manager.created, [])
        record = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["retry_after"])

    def test_first_run_baseline_does_not_require_storage(self):
        result = self.coordinator(
            lambda streamer: {
                "vods": [self.vod("2854443252")] if streamer == "alpha" else []
            },
            storage_provider=lambda settings: AutoVodStorageStatus("insufficient", 1, 200, 50),
        ).run_once()

        self.assertTrue(self.store.baseline_initialized("alpha"))
        self.assertEqual(self.manager.created, [])
        self.assertEqual(result["storage_state"], "not_checked")

    def test_existing_nonterminal_auto_job_blocks_new_jobs_but_keeps_discovery(self):
        self.establish_baseline("alpha", [])
        self.manager.jobs["7"] = {
            "id": "7", "type": "download", "origin": "auto_vod", "state": "running",
            "streamer": "beta", "twitch_vod_id": "2854443200",
            "urls": ["https://www.twitch.tv/videos/2854443200"],
        }

        result = self.coordinator(
            lambda streamer: {
                "vods": [self.vod("2854443252")] if streamer == "alpha" else []
            }
        ).run_once()

        self.assertEqual(result["action"], "waiting_for_existing_job")
        self.assertEqual(result["outstanding_auto_vod_jobs"], 1)
        self.assertEqual(self.manager.created, [])
        self.assertEqual(self.store.get_vod("alpha", "2854443252")["disposition"], "pending")

    def test_terminal_auto_history_and_manual_jobs_do_not_block_new_auto_job(self):
        self.establish_baseline("alpha", [])
        self.manager.jobs["7"] = {
            "id": "7", "type": "download", "origin": "auto_vod", "state": "completed",
            "streamer": "beta", "twitch_vod_id": "2854443200",
            "urls": ["https://www.twitch.tv/videos/2854443200"],
        }
        self.manager.jobs["8"] = {
            "id": "8", "type": "download", "origin": "manual", "state": "running",
            "urls": ["https://www.twitch.tv/videos/2854443100"],
        }

        self.coordinator(
            lambda streamer: {
                "vods": [self.vod("2854443252")] if streamer == "alpha" else []
            }
        ).run_once()

        self.assertEqual([job["twitch_vod_id"] for job in self.manager.created], ["2854443252"])

    def test_repeated_check_now_equivalent_calls_cannot_bypass_storage_guard(self):
        self.establish_baseline("alpha", [])
        low = lambda settings: AutoVodStorageStatus("insufficient", 1, 200, 50)
        discovery = lambda streamer: {
            "vods": [self.vod("2854443252")] if streamer == "alpha" else []
        }

        self.coordinator(discovery, storage_provider=low).run_once()
        self.coordinator(discovery, storage_provider=low).run_once()

        self.assertEqual(self.manager.created, [])
        record = self.store.get_vod("alpha", "2854443252")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["retry_after"])

    def test_shutdown_prevents_job_creation(self):
        self.establish_baseline("alpha", [])
        result = self.coordinator(
            lambda streamer: {"vods": [self.vod("2854443252")]},
            should_stop=lambda: True,
        ).run_once()

        self.assertEqual(result["action"], "shutdown_requested")
        self.assertEqual(self.manager.created, [])


if __name__ == "__main__":
    unittest.main()
