"""Explicit offline migration of Auto VOD state v1 to safe v2 history."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, TextIO

from vod_dashboard.auto_vod import (
    AUTO_VOD_STATE_FILE_NAME,
    AUTO_VOD_STATE_VERSION,
    AutoVodStateLoadError,
    AutoVodStatePersistenceError,
    AutoVodStateStore,
    normalize_auto_vod_state,
    normalize_legacy_auto_vod_state,
)
from vod_dashboard.job_store import JobStore, job_store_path
from vod_dashboard.runtime import ARCHIVE_FILE_NAME
from vod_dashboard.settings import archive_ids_from_path, canonical_streamer_login


class AutoVodMigrationError(RuntimeError):
    """Stable, non-sensitive failure condition for the offline CLI."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MigrationReport:
    action: str
    source_schema: int
    streamer_count: int
    vod_record_count: int
    handled_preserved_count: int = 0
    pending_suppressed_count: int = 0
    retry_pending_suppressed_count: int = 0
    queued_suppressed_count: int = 0
    completed_reconciled_count: int = 0
    cancelled_reconciled_count: int = 0
    completed_jobs_materialized_count: int = 0
    archive_match_count: int = 0
    anomaly_count: int = 0
    baseline_uninitialized_count: int = 0
    backup_status: str = "not_created"

    def safe_lines(self) -> list[str]:
        return [
            f"action={self.action}",
            f"source_schema={self.source_schema}",
            f"streamers={self.streamer_count}",
            f"vod_records={self.vod_record_count}",
            f"handled_preserved={self.handled_preserved_count}",
            f"pending_suppressed={self.pending_suppressed_count}",
            f"retry_pending_suppressed={self.retry_pending_suppressed_count}",
            f"queued_suppressed={self.queued_suppressed_count}",
            f"completed_reconciled={self.completed_reconciled_count}",
            f"cancelled_reconciled={self.cancelled_reconciled_count}",
            f"completed_jobs_materialized={self.completed_jobs_materialized_count}",
            f"archive_matches={self.archive_match_count}",
            f"anomalies={self.anomaly_count}",
            f"baseline_initialized_after_migration=false for {self.baseline_uninitialized_count} streamers",
            f"backup={self.backup_status}",
        ]


@dataclass(frozen=True)
class MigrationPlan:
    dashboard_dir: Path
    state_path: Path
    source_state_bytes: bytes
    jobs_path: Path
    jobs_bytes: Optional[bytes]
    archive_path: Path
    archive_bytes: Optional[bytes]
    converted_state: Optional[Dict[str, Any]]
    report: MigrationReport


def _utc_timestamp(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_dashboard_dir(value: Path) -> Path:
    dashboard_dir = Path(value).expanduser().resolve(strict=False)
    if not dashboard_dir.is_dir():
        raise AutoVodMigrationError("dashboard_dir_invalid")
    return dashboard_dir


def _read_bytes(path: Path, *, required: bool, code: str) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if required:
            raise AutoVodMigrationError(code)
        return None
    except OSError as exc:
        raise AutoVodMigrationError(code) from exc


def _read_json_state(source: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(source.decode("utf-8-sig"))
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise AutoVodMigrationError("state_invalid") from exc
    if not isinstance(value, Mapping):
        raise AutoVodMigrationError("state_invalid")
    return value


def _load_jobs(path: Path) -> tuple[list[Dict[str, Any]], Optional[bytes]]:
    raw = _read_bytes(path, required=False, code="jobs_unreadable")
    result = JobStore(path).load()
    if not result.healthy or result.degraded:
        raise AutoVodMigrationError("jobs_invalid")
    return list(result.jobs), raw


def _load_archive(path: Path) -> tuple[set[str], Optional[bytes]]:
    raw = _read_bytes(path, required=False, code="archive_unreadable")
    if raw is None:
        return set(), None
    try:
        raw.decode("utf-8-sig", errors="ignore")
    except UnicodeDecodeError as exc:  # pragma: no cover - errors="ignore" is defensive
        raise AutoVodMigrationError("archive_invalid") from exc
    return archive_ids_from_path(path), raw


def _auto_vod_job_matches(job: Mapping[str, Any], streamer: str, vod_id: str) -> bool:
    return (
        job.get("type") == "download"
        and job.get("origin") == "auto_vod"
        and canonical_streamer_login(job.get("streamer")) == streamer
        and str(job.get("twitch_vod_id") or "") == vod_id
    )


def _handled_record(
    record: Mapping[str, Any],
    reason: str,
    *,
    job_id: Optional[str] = None,
    preserve_job_id: bool = False,
) -> Dict[str, Any]:
    return {
        "disposition": "handled",
        "reason": reason,
        "attempts": int(record["attempts"]),
        "retry_after": None,
        "job_id": (
            str(record["job_id"])
            if preserve_job_id and record.get("job_id") is not None
            else job_id
        ),
        "discovered_at": str(record["discovered_at"]),
        "updated_at": str(record["updated_at"]),
    }


def _materialized_completed_record(job: Mapping[str, Any]) -> Dict[str, Any]:
    timestamp = str(
        job.get("updated_at")
        or job.get("finished_at")
        or job["created_at"]
    )
    return {
        "disposition": "handled",
        "reason": "downloaded",
        "attempts": int(job.get("attempt") or 0),
        "retry_after": None,
        "job_id": str(job["id"]),
        "discovered_at": str(job["created_at"]),
        "updated_at": timestamp,
    }


def convert_legacy_state(
    legacy_state: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    archive_ids: set[str],
) -> tuple[Dict[str, Any], MigrationReport]:
    """Return an offline-safe v2 conversion without writing any files."""
    legacy = normalize_legacy_auto_vod_state(legacy_state)
    jobs_by_id = {str(job.get("id")): job for job in jobs}
    streamers: Dict[str, Dict[str, Any]] = {}
    counts = {
        "handled_preserved_count": 0,
        "pending_suppressed_count": 0,
        "retry_pending_suppressed_count": 0,
        "queued_suppressed_count": 0,
        "completed_reconciled_count": 0,
        "cancelled_reconciled_count": 0,
        "completed_jobs_materialized_count": 0,
        "archive_match_count": 0,
        "anomaly_count": 0,
    }

    for streamer, legacy_bucket in legacy["streamers"].items():
        target_bucket = {
            "baseline_initialized": False,
            "baseline_established_at": None,
            "vods": {},
        }
        streamers[streamer] = target_bucket
        for vod_id, record in legacy_bucket["vods"].items():
            disposition = record["disposition"]
            if vod_id in archive_ids:
                target_bucket["vods"][vod_id] = _handled_record(
                    record,
                    "downloaded",
                    preserve_job_id=disposition == "handled",
                )
                counts["archive_match_count"] += 1
                continue
            if disposition == "handled":
                target_bucket["vods"][vod_id] = _handled_record(
                    record, str(record["reason"]), preserve_job_id=True
                )
                counts["handled_preserved_count"] += 1
                continue
            if disposition == "pending":
                target_bucket["vods"][vod_id] = _handled_record(
                    record, "legacy_rebaseline_suppressed"
                )
                if record.get("retry_after") is None:
                    counts["pending_suppressed_count"] += 1
                else:
                    counts["retry_pending_suppressed_count"] += 1
                continue

            job = jobs_by_id.get(str(record.get("job_id") or ""))
            if job is None or not _auto_vod_job_matches(job, streamer, vod_id):
                target_bucket["vods"][vod_id] = _handled_record(
                    record, "legacy_rebaseline_suppressed"
                )
                counts["queued_suppressed_count"] += 1
                counts["anomaly_count"] += 1
                continue
            job_state = str(job.get("state") or "")
            if job_state == "completed":
                reason = "downloaded"
                counts["completed_reconciled_count"] += 1
            elif job_state == "cancelled":
                reason = "manual_cancelled"
                counts["cancelled_reconciled_count"] += 1
            else:
                reason = "legacy_rebaseline_suppressed"
                counts["queued_suppressed_count"] += 1
            target_bucket["vods"][vod_id] = _handled_record(
                record, reason, job_id=str(job["id"])
            )

    for job in jobs:
        if (
            job.get("type") != "download"
            or job.get("origin") != "auto_vod"
            or job.get("state") != "completed"
        ):
            continue
        streamer = canonical_streamer_login(job.get("streamer"))
        vod_id = str(job.get("twitch_vod_id") or "")
        if not streamer or not vod_id.isdigit():
            continue
        bucket = streamers.setdefault(
            streamer,
            {
                "baseline_initialized": False,
                "baseline_established_at": None,
                "vods": {},
            },
        )
        if vod_id not in bucket["vods"]:
            bucket["vods"][vod_id] = _materialized_completed_record(job)
            counts["completed_jobs_materialized_count"] += 1

    converted = {"version": AUTO_VOD_STATE_VERSION, "streamers": streamers}
    try:
        normalized = normalize_auto_vod_state(converted)
    except AutoVodStateLoadError as exc:
        raise AutoVodMigrationError("conversion_invalid") from exc
    record_count = sum(
        len(bucket["vods"]) for bucket in normalized["streamers"].values()
    )
    report = MigrationReport(
        action="ready",
        source_schema=1,
        streamer_count=len(normalized["streamers"]),
        vod_record_count=record_count,
        baseline_uninitialized_count=len(normalized["streamers"]),
        **counts,
    )
    return normalized, report


class _MigrationLock:
    def __init__(self, dashboard_dir: Path) -> None:
        self.path = dashboard_dir / ".auto-vod-migrate.lock"
        self._descriptor: Optional[int] = None

    def __enter__(self) -> "_MigrationLock":
        try:
            self._descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.write(self._descriptor, b"offline auto-vod migration\n")
            os.fsync(self._descriptor)
        except FileExistsError as exc:
            raise AutoVodMigrationError("migration_locked") from exc
        except OSError as exc:
            if self._descriptor is not None:
                try:
                    os.close(self._descriptor)
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._descriptor = None
            raise AutoVodMigrationError("migration_lock_failed") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._descriptor is not None:
            try:
                os.close(self._descriptor)
            finally:
                self._descriptor = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_backup_file(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _create_backup(plan: MigrationPlan, *, now: Optional[datetime] = None) -> None:
    timestamp = _utc_timestamp(now)
    base = plan.dashboard_dir / f"auto-vod-migration-backup-{timestamp}"
    backup_dir = base
    suffix = 1
    while backup_dir.exists():
        suffix += 1
        backup_dir = plan.dashboard_dir / f"{base.name}-{suffix}"
    try:
        backup_dir.mkdir(mode=0o700)
        _write_backup_file(
            backup_dir / AUTO_VOD_STATE_FILE_NAME, plan.source_state_bytes
        )
        if plan.jobs_bytes is not None:
            _write_backup_file(backup_dir / plan.jobs_path.name, plan.jobs_bytes)
        if plan.archive_bytes is not None:
            _write_backup_file(backup_dir / plan.archive_path.name, plan.archive_bytes)
        _fsync_directory(backup_dir)
        _fsync_directory(plan.dashboard_dir)
    except Exception as exc:
        raise AutoVodMigrationError("backup_failed") from exc


def _inputs_unchanged(plan: MigrationPlan) -> bool:
    try:
        return all(
            path.read_bytes() == expected
            if expected is not None and path.exists()
            else expected is None and not path.exists()
            for path, expected in (
                (plan.state_path, plan.source_state_bytes),
                (plan.jobs_path, plan.jobs_bytes),
                (plan.archive_path, plan.archive_bytes),
            )
        )
    except OSError:
        return False


def plan_migration(dashboard_dir: Path) -> MigrationPlan:
    """Inspect explicit offline inputs and return a no-write conversion plan."""
    root = _safe_dashboard_dir(dashboard_dir)
    state_path = root / AUTO_VOD_STATE_FILE_NAME
    source = _read_bytes(state_path, required=True, code="state_missing")
    assert source is not None
    state_value = _read_json_state(source)
    version = state_value.get("version")
    if isinstance(version, bool):
        raise AutoVodMigrationError("state_invalid")
    if version == AUTO_VOD_STATE_VERSION:
        try:
            normalized = normalize_auto_vod_state(state_value)
        except AutoVodStateLoadError as exc:
            raise AutoVodMigrationError("state_invalid") from exc
        report = MigrationReport(
            action="already_migrated",
            source_schema=AUTO_VOD_STATE_VERSION,
            streamer_count=len(normalized["streamers"]),
            vod_record_count=sum(
                len(bucket["vods"]) for bucket in normalized["streamers"].values()
            ),
            baseline_uninitialized_count=sum(
                bucket["baseline_initialized"] is False
                for bucket in normalized["streamers"].values()
            ),
        )
        return MigrationPlan(
            root,
            state_path,
            source,
            job_store_path(root),
            None,
            root / ARCHIVE_FILE_NAME,
            None,
            None,
            report,
        )
    if version != 1:
        raise AutoVodMigrationError("state_invalid")
    try:
        legacy = normalize_legacy_auto_vod_state(state_value)
    except AutoVodStateLoadError as exc:
        raise AutoVodMigrationError("state_invalid") from exc
    jobs_path = job_store_path(root)
    jobs, jobs_bytes = _load_jobs(jobs_path)
    archive_path = root / ARCHIVE_FILE_NAME
    archive_ids, archive_bytes = _load_archive(archive_path)
    converted, report = convert_legacy_state(legacy, jobs, archive_ids)
    return MigrationPlan(
        root,
        state_path,
        source,
        jobs_path,
        jobs_bytes,
        archive_path,
        archive_bytes,
        converted,
        report,
    )


def run_migration(
    dashboard_dir: Path,
    *,
    apply: bool,
    now: Optional[datetime] = None,
) -> MigrationReport:
    """Dry-run or apply one explicit offline migration; never touches jobs/archive."""
    if not apply:
        plan = plan_migration(dashboard_dir)
        return MigrationReport(
            **{
                **plan.report.__dict__,
                "action": (
                    "already_migrated"
                    if plan.report.action == "already_migrated"
                    else "dry_run"
                ),
            }
        )

    root = _safe_dashboard_dir(dashboard_dir)
    with _MigrationLock(root):
        plan = plan_migration(root)
        if plan.report.action == "already_migrated":
            return plan.report
        if plan.converted_state is None:
            raise AutoVodMigrationError("conversion_invalid")
        _create_backup(plan, now=now)
        if not _inputs_unchanged(plan):
            raise AutoVodMigrationError("inputs_changed")
        try:
            AutoVodStateStore(plan.state_path).replace_state(
                plan.converted_state, apply_retention=False
            )
        except AutoVodStatePersistenceError as exc:
            raise AutoVodMigrationError("state_persistence_failed") from exc
        return MigrationReport(
            **{**plan.report.__dict__, "action": "applied", "backup_status": "created"}
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-safe Auto VOD state migration. Stop the dashboard before "
            "using --apply. This command never contacts Twitch or rewrites jobs/archive."
        )
    )
    parser.add_argument(
        "--dashboard-dir",
        required=True,
        type=Path,
        help="Explicit dashboard data directory containing auto-vod-state.json.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_migration(args.dashboard_dir, apply=args.apply)
    except AutoVodMigrationError as exc:
        print(
            f"Auto VOD migration failed ({exc.code}). No state was replaced.",
            file=stderr,
        )
        return 1
    for line in report.safe_lines():
        print(line, file=stdout)
    if report.action == "dry_run":
        print("no_files_changed=true", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
