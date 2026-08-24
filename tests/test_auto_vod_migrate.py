import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vod_dashboard.auto_vod import AutoVodStatePersistenceError, AutoVodStateStore
from vod_dashboard.auto_vod_coordinator import AutoVodCoordinator
from vod_dashboard.auto_vod_migrate import (
    AutoVodMigrationError,
    main,
    plan_migration,
    run_migration,
)
from vod_dashboard.job_store import JobStore


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class FakeManager:
    def __init__(self):
        self.jobs = {}
        self.created = []

    def create_download_job(self, urls, label, **metadata):
        job_id = str(len(self.jobs) + 1)
        job = {
            "id": job_id,
            "type": "download",
            "state": "queued",
            "urls": urls,
            **metadata,
        }
        self.jobs[job_id] = job
        self.created.append(job)
        return job_id

    def get_job(self, job_id):
        return self.jobs.get(str(job_id))

    def start_worker(self, target, job_id):
        del target, job_id


class AutoVodMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.temp.name) / "data"
        self.dashboard_dir.mkdir()
        self.state_path = self.dashboard_dir / "auto-vod-state.json"
        self.jobs_path = self.dashboard_dir / "jobs.json"
        self.archive_path = self.dashboard_dir / "archive.txt"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def record(
        disposition,
        *,
        reason=None,
        attempts=0,
        retry_after=None,
        job_id=None,
    ):
        return {
            "disposition": disposition,
            "reason": reason,
            "attempts": attempts,
            "retry_after": retry_after,
            "job_id": job_id,
            "discovered_at": "2026-08-24T00:00:00Z",
            "updated_at": "2026-08-24T00:00:00Z",
        }

    def write_legacy(self, streamers=None):
        value = {
            "version": 1,
            "streamers": streamers
            or {
                "alpha": {
                    "vods": {
                        "2854443252": self.record(
                            "handled", reason="downloaded", attempts=1, job_id="1"
                        ),
                        "2854443251": self.record("pending", reason="new_vod"),
                        "2854443250": self.record(
                            "pending",
                            reason="job_failed",
                            attempts=1,
                            retry_after="2026-08-24T13:00:00Z",
                        ),
                        "2854443249": self.record("queued", job_id="2", attempts=1),
                        "2854443248": self.record("queued", job_id="3", attempts=1),
                        "2854443247": self.record("queued", job_id="4", attempts=1),
                        "2854443246": self.record("queued", job_id="99", attempts=1),
                        "2854443245": self.record("queued", job_id="98", attempts=1),
                    }
                }
            },
        }
        self.state_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return value

    @staticmethod
    def auto_job(job_id, vod_id, state):
        return {
            "id": str(job_id),
            "type": "download",
            "label": "Automatic Twitch VOD: alpha",
            "created_at": "2026-08-24T00:00:00Z",
            "started_at": None,
            "updated_at": "2026-08-24T00:00:00Z",
            "finished_at": None,
            "state": state,
            "completion_reason": "",
            "recovery_reason": "",
            "returncode": None,
            "item_ids": [f"{job_id}-item-1"],
            "item_states": [state],
            "item_completion_reasons": [""],
            "item_recovery_reasons": [""],
            "item_failure_kinds": [""],
            "item_resolved": [False],
            "item_retry_job_ids": [""],
            "urls": [f"https://www.twitch.tv/videos/{vod_id}"],
            "total_urls": 1,
            "item_progress": [None],
            "item_processed_seconds": [None],
            "item_total_duration_seconds": [None],
            "item_updated_at": [None],
            "origin": "auto_vod",
            "streamer": "alpha",
            "twitch_vod_id": str(vod_id),
            "attempt": 1,
            "post_download_mode": "download_only",
        }

    def write_jobs(self, jobs):
        JobStore(self.jobs_path, clock=lambda: NOW).save(
            jobs, next_job_id=100, revision=1
        )

    def test_dry_run_reports_conversion_and_never_writes(self):
        self.write_legacy()
        self.write_jobs(
            [
                self.auto_job("2", "2854443249", "completed"),
                self.auto_job("3", "2854443248", "cancelled"),
                self.auto_job("4", "2854443247", "interrupted"),
                self.auto_job("5", "2854443000", "completed"),
            ]
        )
        self.archive_path.write_text("2854443246\n", encoding="utf-8")
        before = {
            path.name: path.read_bytes()
            for path in (self.state_path, self.jobs_path, self.archive_path)
        }

        report = run_migration(self.dashboard_dir, apply=False)

        self.assertEqual(report.action, "dry_run")
        self.assertEqual(report.source_schema, 1)
        self.assertEqual(report.handled_preserved_count, 1)
        self.assertEqual(report.pending_suppressed_count, 1)
        self.assertEqual(report.retry_pending_suppressed_count, 1)
        self.assertEqual(report.completed_reconciled_count, 1)
        self.assertEqual(report.cancelled_reconciled_count, 1)
        self.assertEqual(report.queued_suppressed_count, 2)
        self.assertEqual(report.archive_match_count, 1)
        self.assertEqual(report.anomaly_count, 1)
        self.assertEqual(report.completed_jobs_materialized_count, 1)
        self.assertEqual(report.baseline_uninitialized_count, 1)
        self.assertEqual(
            before,
            {
                path.name: path.read_bytes()
                for path in (self.state_path, self.jobs_path, self.archive_path)
            },
        )
        self.assertEqual(list(self.dashboard_dir.glob("auto-vod-migration-backup-*")), [])
        self.assertFalse((self.dashboard_dir / ".auto-vod-migrate.lock").exists())

    def test_apply_creates_backup_preserves_inputs_and_converts_records(self):
        self.write_legacy()
        self.write_jobs(
            [
                self.auto_job("2", "2854443249", "completed"),
                self.auto_job("3", "2854443248", "cancelled"),
                self.auto_job("4", "2854443247", "failed"),
                self.auto_job("5", "2854443000", "completed"),
            ]
        )
        self.archive_path.write_text("2854443246\n", encoding="utf-8")
        original_jobs = self.jobs_path.read_bytes()
        original_archive = self.archive_path.read_bytes()
        original_state = self.state_path.read_bytes()

        report = run_migration(
            self.dashboard_dir, apply=True, now=datetime(2026, 8, 24, 13, tzinfo=timezone.utc)
        )

        self.assertEqual(report.action, "applied")
        self.assertEqual(report.backup_status, "created")
        migrated = AutoVodStateStore(self.state_path).load()
        self.assertEqual(migrated["version"], 2)
        bucket = migrated["streamers"]["alpha"]
        self.assertFalse(bucket["baseline_initialized"])
        self.assertIsNone(bucket["baseline_established_at"])
        self.assertEqual(bucket["vods"]["2854443252"]["reason"], "downloaded")
        self.assertEqual(bucket["vods"]["2854443251"]["reason"], "legacy_rebaseline_suppressed")
        self.assertEqual(bucket["vods"]["2854443250"]["retry_after"], None)
        self.assertEqual(bucket["vods"]["2854443249"]["reason"], "downloaded")
        self.assertEqual(bucket["vods"]["2854443248"]["reason"], "manual_cancelled")
        self.assertEqual(bucket["vods"]["2854443247"]["reason"], "legacy_rebaseline_suppressed")
        self.assertEqual(bucket["vods"]["2854443246"]["reason"], "downloaded")
        self.assertEqual(bucket["vods"]["2854443245"]["reason"], "legacy_rebaseline_suppressed")
        self.assertEqual(bucket["vods"]["2854443000"]["reason"], "downloaded")
        self.assertEqual(self.jobs_path.read_bytes(), original_jobs)
        self.assertEqual(self.archive_path.read_bytes(), original_archive)

        backups = list(self.dashboard_dir.glob("auto-vod-migration-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "auto-vod-state.json").read_bytes(), original_state)
        self.assertEqual((backups[0] / "jobs.json").read_bytes(), original_jobs)
        self.assertEqual((backups[0] / "archive.txt").read_bytes(), original_archive)

    def test_backup_or_atomic_persistence_failure_keeps_v1_primary(self):
        self.write_legacy()
        original = self.state_path.read_bytes()
        with mock.patch("vod_dashboard.auto_vod_migrate._create_backup", side_effect=AutoVodMigrationError("backup_failed")):
            with self.assertRaisesRegex(AutoVodMigrationError, "backup_failed"):
                run_migration(self.dashboard_dir, apply=True)
        self.assertEqual(self.state_path.read_bytes(), original)

        with mock.patch.object(
            AutoVodStateStore,
            "replace_state",
            side_effect=AutoVodStatePersistenceError("private disk detail"),
        ):
            with self.assertRaisesRegex(AutoVodMigrationError, "state_persistence_failed"):
                run_migration(self.dashboard_dir, apply=True)
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_invalid_legacy_or_jobs_fails_closed(self):
        self.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(AutoVodMigrationError, "state_invalid"):
            run_migration(self.dashboard_dir, apply=True)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{broken")

        self.write_legacy()
        original = self.state_path.read_bytes()
        self.jobs_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(AutoVodMigrationError, "jobs_invalid"):
            run_migration(self.dashboard_dir, apply=True)
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_v2_reports_already_migrated_without_rewrite(self):
        state = {
            "version": 2,
            "streamers": {
                "alpha": {
                    "baseline_initialized": False,
                    "baseline_established_at": None,
                    "vods": {},
                }
            },
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        original = self.state_path.read_bytes()

        dry = run_migration(self.dashboard_dir, apply=False)
        applied = run_migration(self.dashboard_dir, apply=True)

        self.assertEqual(dry.action, "already_migrated")
        self.assertEqual(applied.action, "already_migrated")
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(list(self.dashboard_dir.glob("auto-vod-migration-backup-*")), [])

    def test_plan_has_no_network_dependency_and_cli_output_is_safe(self):
        self.write_legacy()
        plan = plan_migration(self.dashboard_dir)
        self.assertEqual(plan.report.action, "ready")
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["--dashboard-dir", str(self.dashboard_dir), "--dry-run"],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = stdout.getvalue()
        self.assertIn("action=dry_run", output)
        self.assertIn("no_files_changed=true", output)
        self.assertNotIn("cookie", output.lower())
        self.assertNotIn("token", output.lower())

    def test_post_migration_first_discovery_baselines_then_later_id_queues(self):
        self.write_legacy(
            {
                "alpha": {
                    "vods": {
                        "2854443252": self.record(
                            "handled", reason="downloaded", attempts=1
                        )
                    }
                }
            }
        )
        run_migration(self.dashboard_dir, apply=True)
        store = AutoVodStateStore(self.state_path, clock=lambda: NOW)
        manager = FakeManager()
        settings = {
            "auto_vod_enabled": True,
            "streamer_profiles": {"alpha": {"auto_vod_download": True}},
        }
        discoveries = [
            {"vods": [{"twitch_vod_id": "2854443252"}, {"twitch_vod_id": "2854443251"}]},
            {"vods": [{"twitch_vod_id": "2854443253"}, {"twitch_vod_id": "2854443252"}]},
        ]
        coordinator = AutoVodCoordinator(
            settings_provider=lambda: settings,
            streamer_provider=lambda current: ["alpha"],
            state_store=store,
            job_manager=manager,
            archive_ids_provider=lambda current: set(),
            worker_target=lambda job_id: None,
            discovery=lambda streamer, current, *, limit: discoveries.pop(0),
            clock=lambda: NOW,
        )

        coordinator.run_once()
        self.assertEqual(manager.created, [])
        bucket = store.load()["streamers"]["alpha"]
        self.assertTrue(bucket["baseline_initialized"])
        self.assertEqual(bucket["vods"]["2854443252"]["reason"], "downloaded")
        self.assertEqual(bucket["vods"]["2854443251"]["reason"], "baseline_existing")

        coordinator.run_once()
        self.assertEqual([job["twitch_vod_id"] for job in manager.created], ["2854443253"])
        self.assertEqual(store.get_vod("alpha", "2854443252")["disposition"], "handled")


if __name__ == "__main__":
    unittest.main()
