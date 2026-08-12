"""Flask-independent construction of dashboard and runtime status payloads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Sequence


def directory_is_writable(path: Path) -> bool:
    """Observe writability without creating directories or probe files."""
    candidate = Path(path)
    try:
        if candidate.exists():
            return candidate.is_dir() and os.access(candidate, os.W_OK)

        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                return False
            candidate = parent
        return candidate.is_dir() and os.access(candidate, os.W_OK)
    except OSError:
        return False


def dashboard_status_payload(
    jobs: Sequence[Dict[str, Any]],
    youtube: Dict[str, Any],
    disk: Dict[str, Any],
    upload_mode: str,
    upload_chunk_mb: int,
) -> Dict[str, Any]:
    active = [job for job in jobs if job.get("status") in {"läuft", "wartet"}]
    failed = [job for job in jobs if job.get("status") == "fehler"]
    finished = [job for job in jobs if job.get("status") == "fertig"]
    return {
        "jobs_total": len(jobs),
        "jobs_active": len(active),
        "jobs_failed": len(failed),
        "jobs_finished": len(finished),
        "youtube": youtube,
        "disk": disk,
        "upload_mode": upload_mode,
        "upload_chunk_mb": upload_chunk_mb,
    }


def application_state_payload(
    settings: Dict[str, Any],
    settings_file: str,
    local_settings_file: str,
    persistent_settings_exists: bool,
    streamers: list[str],
    archive_count: int,
    *,
    download_path_exists: bool,
    streamer_file_exists: bool,
    streamer_file_resolved: str,
    streamer_file_forced: Path,
    archive_file_exists: bool,
    archive_file_resolved: str,
    archive_file_forced: Path,
) -> Dict[str, Any]:
    return {
        "settings": settings,
        "settings_file": settings_file,
        "local_settings_file": local_settings_file,
        "persistent_settings_exists": persistent_settings_exists,
        "streamers": streamers,
        "archive_count": archive_count,
        "download_path_exists": download_path_exists,
        "streamer_file_exists": streamer_file_exists,
        "streamer_file_resolved": streamer_file_resolved,
        "streamer_file_forced": str(streamer_file_forced),
        "archive_file_exists": archive_file_exists,
        "archive_file_resolved": archive_file_resolved,
        "archive_file_forced": str(archive_file_forced),
    }


def settings_status_payload(
    settings_file: str,
    settings_exists: bool,
    settings_parent_exists: bool,
    local_settings_file: str,
    legacy_candidates: Sequence[Path],
    download_path: str,
    streamer_file: str,
    archive_file: str,
) -> Dict[str, Any]:
    return {
        "settings_file": settings_file,
        "settings_exists": settings_exists,
        "settings_parent_exists": settings_parent_exists,
        "local_settings_file": local_settings_file,
        "legacy_candidates": [str(path) for path in legacy_candidates[:10]],
        "download_path": download_path,
        "streamer_file": streamer_file,
        "archive_file": archive_file,
    }


def streamer_status_payload(
    fixed_streamer_file: str,
    *,
    exists: bool,
    parent_exists: bool,
    streamers: list[str],
    legacy_candidates: Sequence[Path],
    raw_preview: str,
    has_literal_newlines: bool,
) -> Dict[str, Any]:
    return {
        "streamer_file": fixed_streamer_file,
        "exists": exists,
        "parent_exists": parent_exists,
        "count": len(streamers),
        "streamers": streamers,
        "legacy_candidates": [str(path) for path in legacy_candidates[:10]],
        "raw_preview": raw_preview,
        "has_literal_newlines": has_literal_newlines,
        "note": "Legacy dashboard streamer.txt files are shown for reference but are not read automatically.",
    }
