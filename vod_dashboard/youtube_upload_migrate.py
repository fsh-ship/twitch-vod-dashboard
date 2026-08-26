"""Offline-only migration of Auto YouTube ownership state v1 to v2."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, TextIO

from vod_dashboard.runtime_files import atomic_write_text
from vod_dashboard.youtube_upload_state import (
    LEGACY_YOUTUBE_UPLOAD_STATE_VERSION,
    PART_PLAN_VERSION,
    YOUTUBE_UPLOAD_STATE_FILE_NAME,
    YOUTUBE_UPLOAD_STATE_VERSION,
    YouTubeUploadStateLoadError,
    normalize_legacy_youtube_upload_state,
    normalize_youtube_upload_state,
)


class YouTubeUploadMigrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MigrationReport:
    action: str
    source_schema: int
    records_scanned: int
    records_migrated: int
    confirmed_video_ids_preserved: int = 0
    upload_jobs_preserved: int = 0
    multipart_preparation_required: int = 0
    anomalies: int = 0
    backup_path: Optional[str] = None

    def safe_lines(self) -> list[str]:
        lines = [
            f"action={self.action}", f"source_schema={self.source_schema}",
            f"records_scanned={self.records_scanned}",
            f"records_migrated={self.records_migrated}",
            f"confirmed_video_ids_preserved={self.confirmed_video_ids_preserved}",
            f"upload_jobs_preserved={self.upload_jobs_preserved}",
            f"multipart_preparation_required={self.multipart_preparation_required}",
            f"anomalies={self.anomalies}",
        ]
        if self.backup_path is not None:
            lines.append(f"backup_path={self.backup_path}")
        return lines


@dataclass(frozen=True)
class MigrationPlan:
    dashboard_dir: Path
    state_path: Path
    source_bytes: bytes
    converted: Optional[Dict[str, Any]]
    report: MigrationReport


def _safe_dir(value: Path) -> Path:
    result = Path(value).expanduser().resolve(strict=False)
    if not result.is_dir():
        raise YouTubeUploadMigrationError("dashboard_dir_invalid")
    return result


def _read_state(path: Path) -> tuple[bytes, Mapping[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except FileNotFoundError as exc:
        raise YouTubeUploadMigrationError("state_missing") from exc
    except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise YouTubeUploadMigrationError("state_invalid") from exc
    if not isinstance(value, Mapping):
        raise YouTubeUploadMigrationError("state_invalid")
    return raw, value


def _part_from_confirmed(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Create the sole historic part without fabricating a split manifest."""
    state = "completed" if record["state"] == "completed" else "video_confirmed"
    return {
        "index": 1, "media_path": record["media_path"],
        "size_bytes": record["size_bytes"], "duration_seconds": None,
        "source_kind": "original", "upload_item_id": None,
        "upload_state": state, "attempts": record["attempts"],
        "youtube_video_id": record["youtube_video_id"],
        "playlist_state": record["playlist_state"], "reason": record["reason"],
    }


def convert_v1_state(value: Mapping[str, Any]) -> tuple[Dict[str, Any], MigrationReport]:
    """Convert only fully valid v1 data; no filesystem or JobStore access."""
    try:
        legacy = normalize_legacy_youtube_upload_state(value)
    except YouTubeUploadStateLoadError as exc:
        raise YouTubeUploadMigrationError("state_invalid") from exc
    uploads: Dict[str, Any] = {}
    confirmed = jobs = requires_preparation = 0
    for key, old in legacy["uploads"].items():
        has_video = old["youtube_video_id"] is not None
        parts = [_part_from_confirmed(old)] if has_video else []
        state = old["state"]
        reason = old["reason"]
        # A deferred P8f job is linked but cannot yet be transformed into the
        # future multi-item JobStore contract.  Preserve it and make this
        # explicit for the later materialization slice.
        if old["upload_job_id"] is not None:
            jobs += 1
        if old["upload_job_id"] is not None and not has_video:
            state = "parts_preparing"
            reason = "multipart_preparation_required"
            requires_preparation += 1
        elif has_video:
            confirmed += 1
        elif state == "transfer_started":
            state = "needs_attention"
            reason = "upload_outcome_uncertain"
        uploads[key] = {
            "streamer": old["streamer"], "twitch_vod_id": old["twitch_vod_id"],
            "source_download_job_id": old["source_download_job_id"],
            "source_download_item_id": old["source_download_item_id"],
            "media_path": old["media_path"], "size_bytes": old["size_bytes"],
            "source_duration_seconds": None, "state": state,
            "upload_job_id": old["upload_job_id"], "playlist_id": old["playlist_id"],
            "plan_inputs": old.get("plan_inputs"), "upload_plan": old.get("upload_plan"),
            "part_plan_version": PART_PLAN_VERSION if parts else None,
            "split": None, "parts": parts, "reason": reason,
            "created_at": old["created_at"], "updated_at": old["updated_at"],
        }
    converted = {"version": YOUTUBE_UPLOAD_STATE_VERSION, "uploads": uploads}
    try:
        converted = normalize_youtube_upload_state(converted)
    except YouTubeUploadStateLoadError as exc:
        raise YouTubeUploadMigrationError("conversion_invalid") from exc
    return converted, MigrationReport(
        action="ready", source_schema=LEGACY_YOUTUBE_UPLOAD_STATE_VERSION,
        records_scanned=len(uploads), records_migrated=len(uploads),
        confirmed_video_ids_preserved=confirmed, upload_jobs_preserved=jobs,
        multipart_preparation_required=requires_preparation,
    )


def plan_migration(dashboard_dir: Path) -> MigrationPlan:
    root = _safe_dir(dashboard_dir)
    path = root / YOUTUBE_UPLOAD_STATE_FILE_NAME
    source, value = _read_state(path)
    version = value.get("version")
    if isinstance(version, bool):
        raise YouTubeUploadMigrationError("state_invalid")
    if version == YOUTUBE_UPLOAD_STATE_VERSION:
        try:
            state = normalize_youtube_upload_state(value)
        except YouTubeUploadStateLoadError as exc:
            raise YouTubeUploadMigrationError("state_invalid") from exc
        return MigrationPlan(root, path, source, None, MigrationReport(
            action="already_migrated", source_schema=YOUTUBE_UPLOAD_STATE_VERSION,
            records_scanned=len(state["uploads"]), records_migrated=0,
        ))
    if version != LEGACY_YOUTUBE_UPLOAD_STATE_VERSION:
        raise YouTubeUploadMigrationError("unsupported_schema")
    converted, report = convert_v1_state(value)
    return MigrationPlan(root, path, source, converted, report)


class _MigrationLock:
    def __init__(self, root: Path) -> None:
        self.path = root / ".youtube-upload-migrate.lock"
        self.fd: Optional[int] = None
    def __enter__(self) -> "_MigrationLock":
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(self.fd, b"offline youtube-upload migration\n")
            os.fsync(self.fd)
        except FileExistsError as exc:
            raise YouTubeUploadMigrationError("migration_locked") from exc
        except OSError as exc:
            raise YouTubeUploadMigrationError("migration_lock_failed") from exc
        return self
    def __exit__(self, *_: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def _backup(plan: MigrationPlan, now: Optional[datetime]) -> Path:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = plan.dashboard_dir / f"youtube-upload-migration-backup-{stamp}"
    path = base; suffix = 1
    while path.exists():
        suffix += 1; path = plan.dashboard_dir / f"{base.name}-{suffix}"
    try:
        path.mkdir(mode=0o700)
        backup = path / YOUTUBE_UPLOAD_STATE_FILE_NAME
        with backup.open("xb") as handle:
            handle.write(plan.source_bytes); handle.flush(); os.fsync(handle.fileno())
    except OSError as exc:
        raise YouTubeUploadMigrationError("backup_failed") from exc
    return path


def run_migration(dashboard_dir: Path, *, apply: bool, now: Optional[datetime] = None) -> MigrationReport:
    if not apply:
        plan = plan_migration(dashboard_dir)
        return MigrationReport(**{**plan.report.__dict__, "action": "already_migrated" if plan.converted is None else "dry_run"})
    root = _safe_dir(dashboard_dir)
    with _MigrationLock(root):
        plan = plan_migration(root)
        if plan.converted is None:
            return plan.report
        backup = _backup(plan, now)
        try:
            if plan.state_path.read_bytes() != plan.source_bytes:
                raise YouTubeUploadMigrationError("inputs_changed")
            text = json.dumps(plan.converted, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            atomic_write_text(plan.state_path, text, encoding="utf-8")
        except YouTubeUploadMigrationError:
            raise
        except OSError as exc:
            raise YouTubeUploadMigrationError("state_persistence_failed") from exc
        return MigrationReport(**{**plan.report.__dict__, "action": "applied", "backup_path": str(backup)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline-safe YouTube upload ownership migration. Stop the dashboard before --apply.")
    parser.add_argument("--dashboard-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_migration(args.dashboard_dir, apply=args.apply)
    except YouTubeUploadMigrationError as exc:
        print(f"YouTube upload migration failed ({exc.code}). No state was replaced.", file=stderr)
        return 1
    for line in report.safe_lines(): print(line, file=stdout)
    if report.action == "dry_run": print("no_files_changed=true", file=stdout)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
