"""Isolated, versioned persistence for durable dashboard job snapshots.

This module deliberately has no Flask or JobManager dependency.  It defines
the storage boundary that later roadmap phases can inject into the runtime.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from vod_dashboard.auto_recorder import normalize_auto_recorder_stream_id
from vod_dashboard.settings import canonical_streamer_login


JOB_STORE_FILE_NAME = "jobs.json"
JOB_STORE_VERSION = 1
TERMINAL_JOB_LIMIT = 100
MAX_STORED_JOBS = 10_000
MAX_ITEMS_PER_JOB = 1_000
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_JOB_ID = 9_999_999_999
MAX_NEXT_JOB_ID = MAX_JOB_ID + 1
SAFE_NEXT_JOB_ID_FALLBACK = 1_000_000_000
MAX_REVISION = 9_007_199_254_740_991
MAX_LABEL_LENGTH = 500
MAX_TITLE_LENGTH = 1_000
MAX_PATH_LENGTH = 1_024
MAX_PLAYLIST_ID_LENGTH = 256
MAX_REASON_LENGTH = 64
MAX_RETURN_CODE = 2_147_483_647
MAX_SECONDS = 315_576_000.0  # ten years
MAX_BYTES = 9_223_372_036_854_775_807

JOB_TYPES = frozenset({"download", "youtube_upload", "recording"})
JOB_STATES = frozenset(
    {
        "queued",
        "running",
        "stopping",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
)
TERMINAL_JOB_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
FAILURE_KINDS = frozenset({"", "known", "uncertain"})
RECORDING_ORIGINS = frozenset({"manual", "auto"})
DOWNLOAD_ORIGINS = frozenset({"manual", "auto_vod"})
DOWNLOAD_POST_MODES = frozenset({"default", "download_only"})
COMPLETED_MEDIA_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
)
AUTO_YOUTUBE_HANDOFF_STATES = frozenset(
    {"not_eligible", "intent_pending", "intent_created", "handoff_blocked"}
)
AUTO_YOUTUBE_HANDOFF_REASONS = frozenset(
    {
        "",
        "global_disabled",
        "streamer_disabled",
        "settings_unavailable",
        "upload_state_unhealthy",
        "intent_persistence_failed",
        "intent_conflict",
        "intent_missing",
        "invalid_completed_result",
    }
)
AUTO_VOD_STORAGE_BLOCK_REASONS = frozenset(
    {"insufficient_storage", "storage_unavailable"}
)

_JOB_ID_RE = re.compile(r"[1-9][0-9]{0,9}")
_ITEM_ID_RE = re.compile(r"([1-9][0-9]{0,9})-item-([1-9][0-9]{0,3})")
_REASON_RE = re.compile(rf"[a-z][a-z0-9_]{{0,{MAX_REASON_LENGTH - 1}}}")
_PLAYLIST_ID_RE = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MAX_PLAYLIST_ID_LENGTH}}}"
)
_TWITCH_VOD_URL_RE = re.compile(
    r"https://www\.twitch\.tv/videos/([1-9][0-9]{5,19})"
)
_VOD_ID_RE = re.compile(r"[1-9][0-9]{0,31}")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

Job = Dict[str, Any]
State = Dict[str, Any]
Clock = Callable[[], datetime]


class JobStoreError(RuntimeError):
    """Base error for isolated job persistence."""


class JobStoreValidationError(JobStoreError, ValueError):
    """Raised before saving an unsafe or malformed outgoing snapshot."""


class JobStorePersistenceError(JobStoreError):
    """Raised when an already validated snapshot cannot be atomically saved."""


@dataclass(frozen=True)
class JobStoreLoadResult:
    """Safe result for missing, healthy, or degraded persisted history."""

    state: State
    jobs: list[Job]
    next_job_id: int
    healthy: bool
    degraded: bool
    source: str
    reason: str
    discarded_job_count: int


@dataclass(frozen=True)
class JobStoreSaveResult:
    """Result of one ordered persistence attempt."""

    state: State
    saved: bool
    stale: bool
    revision: int


def job_store_path(dashboard_dir: Path) -> Path:
    """Return the fixed store path below the persistent dashboard directory."""

    return Path(dashboard_dir) / JOB_STORE_FILE_NAME


def empty_job_store_state() -> State:
    """Return the in-memory representation of a never-saved empty store."""

    return {
        "version": JOB_STORE_VERSION,
        "next_job_id": 1,
        "saved_at": None,
        "jobs": [],
    }


def normalize_utc_timestamp(value: Any) -> Optional[str]:
    """Normalize a timezone-aware ISO timestamp to second-precision UTC."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _timestamp_from_clock(clock: Clock) -> str:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise JobStoreValidationError("invalid_clock")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _safe_text(
    value: Any,
    *,
    maximum: int,
    required: bool = False,
    code: str = "invalid_text",
) -> str:
    if not isinstance(value, str):
        raise JobStoreValidationError(code)
    candidate = value.strip()
    if (required and not candidate) or len(candidate) > maximum:
        raise JobStoreValidationError(code)
    if _CONTROL_RE.search(candidate):
        raise JobStoreValidationError(code)
    return candidate


def _optional_timestamp(value: Any, code: str) -> Optional[str]:
    if value is None:
        return None
    normalized = normalize_utc_timestamp(value)
    if normalized is None:
        raise JobStoreValidationError(code)
    return normalized


def _required_timestamp(value: Any, code: str) -> str:
    normalized = normalize_utc_timestamp(value)
    if normalized is None:
        raise JobStoreValidationError(code)
    return normalized


def _job_id(value: Any, code: str = "invalid_job_id") -> str:
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise JobStoreValidationError(code)
    number = int(value)
    if number < 1 or number > MAX_JOB_ID:
        raise JobStoreValidationError(code)
    return value


def _next_job_id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_NEXT_JOB_ID else None


def _bounded_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    code: str,
    nullable: bool = False,
) -> Optional[int]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise JobStoreValidationError(code)
    if value < minimum or value > maximum:
        raise JobStoreValidationError(code)
    return value


def _bounded_number(
    value: Any,
    *,
    minimum: float,
    maximum: float,
    code: str,
    nullable: bool = False,
) -> Optional[float]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobStoreValidationError(code)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise JobStoreValidationError(code)
    return number


def _reason(value: Any, code: str, *, nullable: bool = False) -> Optional[str]:
    if value is None and nullable:
        return None
    if value == "":
        return ""
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise JobStoreValidationError(code)
    return value


def _playlist_id(value: Any, code: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _PLAYLIST_ID_RE.fullmatch(value):
        raise JobStoreValidationError(code)
    return value


def _relative_media_path(
    value: Any,
    *,
    media_root: Optional[Path] = None,
    code: str = "invalid_media_path",
) -> str:
    if not isinstance(value, str):
        raise JobStoreValidationError(code)
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > MAX_PATH_LENGTH
        or _CONTROL_RE.search(candidate)
        or "://" in candidate
        or candidate.startswith(("//", "\\\\"))
    ):
        raise JobStoreValidationError(code)

    raw_path = Path(candidate)
    drive_path = bool(_WINDOWS_DRIVE_RE.match(candidate))
    if raw_path.is_absolute() or drive_path:
        if media_root is None:
            raise JobStoreValidationError(code)
        try:
            root = Path(media_root).resolve()
            resolved = raw_path.resolve()
            candidate = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise JobStoreValidationError(code) from exc

    normalized_input = candidate.replace("\\", "/")
    path = PurePosixPath(normalized_input)
    if path.is_absolute() or not path.parts:
        raise JobStoreValidationError(code)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise JobStoreValidationError(code)
    normalized = path.as_posix()
    if not normalized or len(normalized) > MAX_PATH_LENGTH:
        raise JobStoreValidationError(code)
    return normalized


def _filename(value: Any, code: str) -> str:
    if value is None or value == "":
        return ""
    name = _safe_text(value, maximum=255, code=code)
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise JobStoreValidationError(code)
    return name


def _aligned_list(
    value: Any,
    count: int,
    code: str,
    *,
    default: Optional[Callable[[], Any]] = None,
) -> list[Any]:
    if value is None and default is not None:
        return [default() for _ in range(count)]
    if not isinstance(value, list) or len(value) != count:
        raise JobStoreValidationError(code)
    return value


def _normalize_common(
    job: Mapping[str, Any], *, allow_implicit_download: bool
) -> tuple[Job, int]:
    job_id = _job_id(job.get("id"))
    raw_type = job.get("type")
    job_type = "download" if allow_implicit_download and not raw_type else raw_type
    if not isinstance(job_type, str) or job_type not in JOB_TYPES:
        raise JobStoreValidationError("unknown_job_type")
    state = job.get("state")
    if not isinstance(state, str) or state not in JOB_STATES:
        raise JobStoreValidationError("invalid_job_state")
    item_ids = job.get("item_ids")
    item_states = job.get("item_states")
    if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= MAX_ITEMS_PER_JOB:
        raise JobStoreValidationError("invalid_item_ids")
    count = len(item_ids)
    if not isinstance(item_states, list) or len(item_states) != count:
        raise JobStoreValidationError("misaligned_item_states")
    expected_ids = [f"{job_id}-item-{index + 1}" for index in range(count)]
    if item_ids != expected_ids or any(
        not isinstance(value, str) or value not in JOB_STATES
        for value in item_states
    ):
        raise JobStoreValidationError("invalid_item_identity")

    completion_reasons = _aligned_list(
        job.get("item_completion_reasons"),
        count,
        "misaligned_item_completion_reasons",
        default=lambda: "",
    )
    recovery_reasons = _aligned_list(
        job.get("item_recovery_reasons"),
        count,
        "misaligned_item_recovery_reasons",
        default=lambda: "",
    )
    failure_kinds = _aligned_list(
        job.get("item_failure_kinds"),
        count,
        "misaligned_item_failure_kinds",
        default=lambda: "",
    )
    resolved = _aligned_list(
        job.get("item_resolved"),
        count,
        "misaligned_item_resolved",
        default=lambda: False,
    )
    retry_ids = _aligned_list(
        job.get("item_retry_job_ids"),
        count,
        "misaligned_item_retry_job_ids",
        default=lambda: "",
    )

    normalized_retry_ids: list[str] = []
    for value in retry_ids:
        if value == "" or value == "__pending__":
            normalized_retry_ids.append("")
        else:
            normalized_retry_ids.append(_job_id(value, "invalid_retry_job_id"))

    result: Job = {
        "id": job_id,
        "type": job_type,
        "label": _safe_text(
            job.get("label"), maximum=MAX_LABEL_LENGTH, required=True, code="invalid_label"
        ),
        "created_at": _required_timestamp(job.get("created_at"), "invalid_created_at"),
        "started_at": _optional_timestamp(job.get("started_at"), "invalid_started_at"),
        "updated_at": _optional_timestamp(job.get("updated_at"), "invalid_updated_at"),
        "finished_at": _optional_timestamp(job.get("finished_at"), "invalid_finished_at"),
        "state": state,
        "completion_reason": _reason(
            job.get("completion_reason", ""), "invalid_completion_reason"
        ),
        "recovery_reason": _reason(
            job.get("recovery_reason", ""), "invalid_recovery_reason"
        ),
        "returncode": _bounded_int(
            job.get("returncode"),
            minimum=-MAX_RETURN_CODE,
            maximum=MAX_RETURN_CODE,
            code="invalid_returncode",
            nullable=True,
        ),
        "item_ids": list(item_ids),
        "item_states": list(item_states),
        "item_completion_reasons": [
            _reason(value, "invalid_item_completion_reason")
            for value in completion_reasons
        ],
        "item_recovery_reasons": [
            _reason(value, "invalid_item_recovery_reason")
            for value in recovery_reasons
        ],
        "item_failure_kinds": [],
        "item_resolved": [],
        "item_retry_job_ids": normalized_retry_ids,
    }
    for value in failure_kinds:
        if not isinstance(value, str) or value not in FAILURE_KINDS:
            raise JobStoreValidationError("invalid_item_failure_kind")
        result["item_failure_kinds"].append(value)
    for value in resolved:
        if not isinstance(value, bool):
            raise JobStoreValidationError("invalid_item_resolved")
        result["item_resolved"].append(value)

    retry_of = job.get("retry_of")
    if retry_of is not None:
        if not isinstance(retry_of, Mapping):
            raise JobStoreValidationError("invalid_retry_of")
        parent_job_id = _job_id(retry_of.get("job_id"), "invalid_retry_of")
        parent_item_id = retry_of.get("item_id")
        match = _ITEM_ID_RE.fullmatch(parent_item_id) if isinstance(parent_item_id, str) else None
        if match is None or match.group(1) != parent_job_id:
            raise JobStoreValidationError("invalid_retry_of")
        result["retry_of"] = {
            "job_id": parent_job_id,
            "item_id": parent_item_id,
        }
    return result, count


def _numeric_array(
    job: Mapping[str, Any],
    key: str,
    count: int,
    *,
    minimum: float,
    maximum: float,
) -> list[Optional[float]]:
    values = _aligned_list(
        job.get(key), count, f"misaligned_{key}", default=lambda: None
    )
    return [
        _bounded_number(
            value,
            minimum=minimum,
            maximum=maximum,
            code=f"invalid_{key}",
            nullable=True,
        )
        for value in values
    ]


def _integer_array(
    job: Mapping[str, Any], key: str, count: int
) -> list[Optional[int]]:
    values = _aligned_list(
        job.get(key), count, f"misaligned_{key}", default=lambda: None
    )
    return [
        _bounded_int(
            value,
            minimum=0,
            maximum=MAX_BYTES,
            code=f"invalid_{key}",
            nullable=True,
        )
        for value in values
    ]


def _normalize_download(job: Mapping[str, Any], result: Job, count: int) -> None:
    urls = job.get("urls")
    if not isinstance(urls, list) or len(urls) != count:
        raise JobStoreValidationError("misaligned_download_urls")
    normalized_urls = []
    for value in urls:
        if not isinstance(value, str) or not _TWITCH_VOD_URL_RE.fullmatch(value):
            raise JobStoreValidationError("invalid_twitch_vod_url")
        normalized_urls.append(value)
    total_urls = job.get("total_urls", count)
    if total_urls != count:
        raise JobStoreValidationError("invalid_total_urls")
    origin = job.get("origin", "manual")
    if not isinstance(origin, str) or origin not in DOWNLOAD_ORIGINS:
        raise JobStoreValidationError("invalid_download_origin")
    streamer = canonical_streamer_login(job.get("streamer", ""))
    vod_id = job.get("twitch_vod_id", "")
    if not isinstance(vod_id, str) or (vod_id and not _VOD_ID_RE.fullmatch(vod_id)):
        raise JobStoreValidationError("invalid_download_vod_id")
    display_title = _safe_text(job.get("display_title", ""), maximum=500, code="invalid_display_title")
    attempt = _bounded_int(
        job.get("attempt", 0), minimum=0, maximum=1_000,
        code="invalid_download_attempt",
    )
    post_download_mode = job.get("post_download_mode", "default")
    if not isinstance(post_download_mode, str) or post_download_mode not in DOWNLOAD_POST_MODES:
        raise JobStoreValidationError("invalid_post_download_mode")
    if origin == "auto_vod" and (
        not streamer or not vod_id or attempt < 1 or post_download_mode != "download_only"
    ):
        raise JobStoreValidationError("invalid_auto_vod_download_metadata")
    retry_context = "retry_of" in result
    if origin == "manual" and (attempt != 0 or post_download_mode != "default"):
        raise JobStoreValidationError("invalid_manual_download_metadata")
    if origin == "manual" and (streamer or vod_id) and not retry_context:
        raise JobStoreValidationError("invalid_manual_download_metadata")
    storage_blocked = job.get("storage_blocked", False)
    blocking_reason = job.get("blocking_reason", "")
    if not isinstance(storage_blocked, bool):
        raise JobStoreValidationError("invalid_storage_blocked")
    if not isinstance(blocking_reason, str):
        raise JobStoreValidationError("invalid_blocking_reason")
    if storage_blocked:
        if (
            origin != "auto_vod"
            or blocking_reason not in AUTO_VOD_STORAGE_BLOCK_REASONS
            or result.get("state") not in {"queued", "running"}
            or any(state not in {"queued", "running"} for state in result["item_states"])
        ):
            raise JobStoreValidationError("invalid_storage_block")
    elif blocking_reason:
        raise JobStoreValidationError("invalid_storage_block")
    result.update(
        {
            "urls": normalized_urls,
            "total_urls": count,
            "item_progress": _numeric_array(
                job, "item_progress", count, minimum=0.0, maximum=100.0
            ),
            "item_processed_seconds": _numeric_array(
                job,
                "item_processed_seconds",
                count,
                minimum=0.0,
                maximum=MAX_SECONDS,
            ),
            "item_total_duration_seconds": _numeric_array(
                job,
                "item_total_duration_seconds",
                count,
                minimum=0.0,
                maximum=MAX_SECONDS,
            ),
            "item_updated_at": _numeric_array(
                job,
                "item_updated_at",
                count,
                minimum=0.0,
                maximum=MAX_BYTES,
            ),
        }
    )
    if origin == "auto_vod" or retry_context:
        result.update(
            {
                "streamer": streamer,
                "twitch_vod_id": vod_id,
                "display_title": display_title,
            }
        )
    if origin == "auto_vod":
        result.update({"origin": origin, "attempt": attempt, "post_download_mode": post_download_mode, "storage_blocked": storage_blocked, "blocking_reason": blocking_reason})
    completed_values = {
        "completed_media_path": job.get("completed_media_path"),
        "completed_media_size_bytes": job.get("completed_media_size_bytes"),
        "completed_twitch_vod_id": job.get("completed_twitch_vod_id"),
    }
    if any(value is not None for value in completed_values.values()):
        if (
            origin != "auto_vod"
            or count != 1
            or result["item_states"] != ["completed"]
            or not all(value is not None for value in completed_values.values())
        ):
            raise JobStoreValidationError("invalid_completed_media_result")
        raw_path = completed_values["completed_media_path"]
        if not isinstance(raw_path, str):
            raise JobStoreValidationError("invalid_completed_media_path")
        candidate_path = raw_path.strip().replace("\\", "/")
        if Path(candidate_path).is_absolute() or _WINDOWS_DRIVE_RE.match(candidate_path):
            raise JobStoreValidationError("invalid_completed_media_path")
        completed_path = _relative_media_path(
            candidate_path,
            media_root=None,
            code="invalid_completed_media_path",
        )
        if (
            PurePosixPath(completed_path).suffix.lower()
            not in COMPLETED_MEDIA_EXTENSIONS
        ):
            raise JobStoreValidationError("invalid_completed_media_path")
        completed_size = _bounded_int(
            completed_values["completed_media_size_bytes"],
            minimum=0,
            maximum=MAX_BYTES,
            code="invalid_completed_media_size_bytes",
        )
        completed_vod_id = completed_values["completed_twitch_vod_id"]
        if (
            not isinstance(completed_vod_id, str)
            or not _VOD_ID_RE.fullmatch(completed_vod_id)
            or completed_vod_id != vod_id
        ):
            raise JobStoreValidationError("invalid_completed_twitch_vod_id")
        result.update(
            {
                "completed_media_path": completed_path,
                "completed_media_size_bytes": completed_size,
                "completed_twitch_vod_id": completed_vod_id,
            }
        )
    handoff_values = {
        "item_auto_youtube_handoffs": job.get(
            "item_auto_youtube_handoffs"
        ),
        "item_auto_youtube_handoff_reasons": job.get(
            "item_auto_youtube_handoff_reasons"
        ),
        "item_auto_youtube_playlist_ids": job.get(
            "item_auto_youtube_playlist_ids"
        ),
    }
    if any(value is not None for value in handoff_values.values()):
        if (
            origin != "auto_vod"
            or count != 1
            or not all(value is not None for value in handoff_values.values())
            or not all(value is not None for value in completed_values.values())
        ):
            raise JobStoreValidationError("invalid_auto_youtube_handoff")
        handoffs = _aligned_list(
            handoff_values["item_auto_youtube_handoffs"],
            count,
            "misaligned_item_auto_youtube_handoffs",
            default=lambda: "",
        )
        reasons = _aligned_list(
            handoff_values["item_auto_youtube_handoff_reasons"],
            count,
            "misaligned_item_auto_youtube_handoff_reasons",
            default=lambda: "",
        )
        playlists = _aligned_list(
            handoff_values["item_auto_youtube_playlist_ids"],
            count,
            "misaligned_item_auto_youtube_playlist_ids",
            default=lambda: "",
        )
        handoff = handoffs[0]
        reason = reasons[0]
        playlist_id = playlists[0]
        if (
            not isinstance(handoff, str)
            or handoff not in AUTO_YOUTUBE_HANDOFF_STATES
            or not isinstance(reason, str)
            or reason not in AUTO_YOUTUBE_HANDOFF_REASONS
            or not isinstance(playlist_id, str)
            or (playlist_id and not _PLAYLIST_ID_RE.fullmatch(playlist_id))
        ):
            raise JobStoreValidationError("invalid_auto_youtube_handoff")
        if result["item_states"] != ["completed"]:
            raise JobStoreValidationError("invalid_auto_youtube_handoff")
        if handoff == "not_eligible":
            if reason not in {"global_disabled", "streamer_disabled", "settings_unavailable"} or playlist_id:
                raise JobStoreValidationError("invalid_auto_youtube_handoff")
        elif handoff in {"intent_pending", "intent_created"}:
            if reason:
                raise JobStoreValidationError("invalid_auto_youtube_handoff")
        elif handoff == "handoff_blocked" and reason not in {
            "upload_state_unhealthy",
            "intent_persistence_failed",
            "intent_conflict",
            "intent_missing",
            "invalid_completed_result",
            "settings_unavailable",
        }:
            raise JobStoreValidationError("invalid_auto_youtube_handoff")
        result.update(
            {
                "item_auto_youtube_handoffs": [handoff],
                "item_auto_youtube_handoff_reasons": [reason],
                "item_auto_youtube_playlist_ids": [playlist_id],
            }
        )


def _normalize_upload_metadata(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JobStoreValidationError("invalid_upload_metadata")
    streamer_value = value.get("streamer", "")
    streamer = ""
    if streamer_value:
        streamer = canonical_streamer_login(streamer_value)
        if not streamer:
            raise JobStoreValidationError("invalid_upload_streamer")
    vod_id_value = value.get("vod_id", "")
    if vod_id_value and (
        not isinstance(vod_id_value, str) or not _VOD_ID_RE.fullmatch(vod_id_value)
    ):
        raise JobStoreValidationError("invalid_upload_vod_id")
    return {
        "streamer": streamer,
        "date": _safe_text(
            value.get("date", ""), maximum=64, code="invalid_upload_date"
        ),
        "title": _safe_text(
            value.get("title", ""), maximum=MAX_TITLE_LENGTH, code="invalid_upload_title"
        ),
        "vod_id": vod_id_value,
        "name": _filename(value.get("name", ""), "invalid_upload_filename"),
        "size_bytes": _bounded_int(
            value.get("size_bytes"),
            minimum=0,
            maximum=MAX_BYTES,
            code="invalid_upload_size_bytes",
            nullable=True,
        ),
        "size_gb": _bounded_number(
            value.get("size_gb"),
            minimum=0.0,
            maximum=float(MAX_BYTES),
            code="invalid_upload_size_gb",
            nullable=True,
        ),
        "youtube_playlist_id": _playlist_id(
            value.get("youtube_playlist_id", ""), "invalid_item_playlist_id"
        ),
    }


def _normalize_upload(
    job: Mapping[str, Any],
    result: Job,
    count: int,
    *,
    media_root: Optional[Path],
) -> None:
    paths = job.get("urls")
    if not isinstance(paths, list) or len(paths) != count:
        raise JobStoreValidationError("misaligned_upload_paths")
    metadata = _aligned_list(
        job.get("item_metadata"),
        count,
        "misaligned_item_metadata",
        default=lambda: {},
    )
    result.update(
        {
            "urls": [
                _relative_media_path(value, media_root=media_root)
                for value in paths
            ],
            "playlist_id": _playlist_id(
                job.get("playlist_id", ""), "invalid_playlist_id"
            ),
            "item_metadata": [
                _normalize_upload_metadata(value) for value in metadata
            ],
            "item_progress": _numeric_array(
                job, "item_progress", count, minimum=0.0, maximum=100.0
            ),
            "item_bytes_uploaded": _integer_array(
                job, "item_bytes_uploaded", count
            ),
            "item_total_bytes": _integer_array(job, "item_total_bytes", count),
            "item_updated_at": _numeric_array(
                job,
                "item_updated_at",
                count,
                minimum=0.0,
                maximum=MAX_BYTES,
            ),
        }
    )


def _normalize_recording(
    job: Mapping[str, Any],
    result: Job,
    count: int,
    *,
    media_root: Optional[Path],
) -> None:
    if count != 1:
        raise JobStoreValidationError("invalid_recording_item_count")
    streamer = canonical_streamer_login(job.get("streamer"))
    if not streamer:
        raise JobStoreValidationError("invalid_recording_streamer")
    stream_id_value = job.get("stream_id", "")
    stream_id = normalize_auto_recorder_stream_id(stream_id_value)
    if stream_id_value and not stream_id:
        raise JobStoreValidationError("invalid_recording_stream_id")
    origin = job.get("origin")
    if not isinstance(origin, str) or origin not in RECORDING_ORIGINS:
        raise JobStoreValidationError("invalid_recording_origin")
    attempt = _bounded_int(
        job.get("attempt"),
        minimum=1,
        maximum=1_000,
        code="invalid_recording_attempt",
    )
    output_path_value = job.get("output_path")
    output_path = (
        _relative_media_path(output_path_value, media_root=media_root)
        if output_path_value is not None
        else None
    )
    output_complete = job.get("output_complete", False)
    stop_requested = job.get("stop_requested", False)
    if not isinstance(output_complete, bool) or not isinstance(stop_requested, bool):
        raise JobStoreValidationError("invalid_recording_flags")
    result.update(
        {
            "streamer": streamer,
            "stream_id": stream_id,
            "origin": origin,
            "attempt": attempt,
            "title": _safe_text(
                job.get("title", ""),
                maximum=MAX_TITLE_LENGTH,
                code="invalid_recording_title",
            ),
            "live_started_at": _optional_timestamp(
                job.get("live_started_at"), "invalid_live_started_at"
            ),
            "quality": _safe_text(
                job.get("quality", "source/best"),
                maximum=128,
                required=True,
                code="invalid_recording_quality",
            ),
            "output_name": _relative_media_path(
                job.get("output_name"), media_root=media_root, code="invalid_output_name"
            ),
            "output_path": output_path,
            "output_complete": output_complete,
            "recorded_seconds": _bounded_number(
                job.get("recorded_seconds", 0.0),
                minimum=0.0,
                maximum=MAX_SECONDS,
                code="invalid_recorded_seconds",
            ),
            "stop_requested": stop_requested,
        }
    )


def _normalize_job(
    value: Any,
    *,
    media_root: Optional[Path] = None,
    allow_implicit_download: bool = False,
) -> Job:
    if not isinstance(value, Mapping):
        raise JobStoreValidationError("invalid_job_structure")
    result, count = _normalize_common(
        value, allow_implicit_download=allow_implicit_download
    )
    if result["type"] == "download":
        _normalize_download(value, result, count)
    elif result["type"] == "youtube_upload":
        _normalize_upload(value, result, count, media_root=media_root)
    else:
        _normalize_recording(value, result, count, media_root=media_root)
    return result


def serialize_job(job: Mapping[str, Any], *, media_root: Optional[Path] = None) -> Job:
    """Strictly serialize one current job through the version-1 allowlist."""

    return _normalize_job(
        job, media_root=media_root, allow_implicit_download=True
    )


def normalize_persisted_job(data: Any) -> Optional[Job]:
    """Safely normalize one stored entry, discarding malformed history."""

    try:
        return _normalize_job(data)
    except JobStoreValidationError:
        return None


def _retention_timestamp(job: Mapping[str, Any]) -> float:
    for key in ("finished_at", "updated_at", "created_at"):
        normalized = normalize_utc_timestamp(job.get(key))
        if normalized is not None:
            return datetime.fromisoformat(
                normalized[:-1] + "+00:00"
            ).timestamp()
    return 0.0


def apply_retention(
    jobs: Iterable[Job], *, terminal_limit: int = TERMINAL_JOB_LIMIT
) -> list[Job]:
    """Retain all active jobs and the deterministic newest terminal history."""

    values = list(jobs)
    if terminal_limit < 0:
        raise JobStoreValidationError("invalid_terminal_limit")
    protected_pending_handoff = {
        str(job.get("id") or "")
        for job in values
        if (
            job.get("origin") == "auto_vod"
            and "intent_pending"
            in (job.get("item_auto_youtube_handoffs") or [])
        )
    }
    terminal = [
        job
        for job in values
        if (
            job.get("state") in TERMINAL_JOB_STATES
            and str(job.get("id") or "") not in protected_pending_handoff
        )
    ]
    terminal.sort(key=lambda job: (_retention_timestamp(job), int(job["id"])))
    retained_terminal_ids = {
        job["id"] for job in terminal[-terminal_limit:]
    } if terminal_limit else set()
    return [
        job
        for job in values
        if job.get("state") not in TERMINAL_JOB_STATES
        or job["id"] in retained_terminal_ids
        or str(job.get("id") or "") in protected_pending_handoff
    ]


def _effective_next_job_id(raw: Any, jobs: list[Job]) -> tuple[int, bool]:
    maximum = max((int(job["id"]) for job in jobs), default=0)
    minimum = maximum + 1
    normalized = _next_job_id(raw)
    if normalized is None:
        return max(minimum, SAFE_NEXT_JOB_ID_FALLBACK), True
    return max(normalized, minimum), normalized < minimum


def _state_from_loaded_value(value: Any) -> tuple[State, int, str]:
    if not isinstance(value, Mapping):
        raise JobStoreValidationError("invalid_structure")
    version = value.get("version")
    if isinstance(version, bool) or version != JOB_STORE_VERSION:
        raise JobStoreValidationError("unsupported_version")
    saved_at = normalize_utc_timestamp(value.get("saved_at"))
    if saved_at is None:
        raise JobStoreValidationError("invalid_saved_at")
    raw_jobs = value.get("jobs")
    if not isinstance(raw_jobs, list) or len(raw_jobs) > MAX_STORED_JOBS:
        raise JobStoreValidationError("invalid_jobs")

    jobs: list[Job] = []
    seen: set[str] = set()
    discarded = 0
    for raw_job in raw_jobs:
        job = normalize_persisted_job(raw_job)
        if job is None or job["id"] in seen:
            discarded += 1
            continue
        seen.add(job["id"])
        jobs.append(job)
    next_job_id, corrected = _effective_next_job_id(value.get("next_job_id"), jobs)
    jobs = apply_retention(jobs)
    reason = ""
    if discarded and corrected:
        reason = "partial_recovery"
    elif discarded:
        reason = "discarded_jobs"
    elif corrected:
        reason = "invalid_next_job_id"
    return {
        "version": JOB_STORE_VERSION,
        "next_job_id": next_job_id,
        "saved_at": saved_at,
        "jobs": jobs,
    }, discarded, reason


class JobStore:
    """Thread-safe isolated version-1 JSON job history store."""

    def __init__(self, path: Path, *, clock: Optional[Clock] = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._last_written_revision = -1
        self._last_load_result: Optional[JobStoreLoadResult] = None
        self._last_save_at: Optional[str] = None
        self._last_error_code = ""

    @classmethod
    def from_dashboard_dir(
        cls, dashboard_dir: Path, *, clock: Optional[Clock] = None
    ) -> "JobStore":
        return cls(job_store_path(dashboard_dir), clock=clock)

    def _load_result(
        self,
        state: State,
        *,
        healthy: bool,
        degraded: bool,
        source: str,
        reason: str,
        discarded: int,
    ) -> JobStoreLoadResult:
        detached = deepcopy(state)
        return JobStoreLoadResult(
            state=detached,
            jobs=deepcopy(detached["jobs"]),
            next_job_id=int(detached["next_job_id"]),
            healthy=healthy,
            degraded=degraded,
            source=source,
            reason=reason,
            discarded_job_count=discarded,
        )

    def load(self) -> JobStoreLoadResult:
        """Load usable history without throwing for ordinary file corruption."""

        with self._lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                result = self._load_result(
                    empty_job_store_state(),
                    healthy=True,
                    degraded=False,
                    source="empty",
                    reason="missing",
                    discarded=0,
                )
                self._last_load_result = result
                self._last_error_code = ""
                return result
            except OSError:
                size = -1
            if size < 0:
                reason = "unreadable_state"
            elif size > MAX_STORE_BYTES:
                reason = "file_too_large"
            else:
                try:
                    raw = self.path.read_text(encoding="utf-8-sig")
                except OSError:
                    reason = "unreadable_state"
                else:
                    try:
                        value = json.loads(raw)
                    except (TypeError, ValueError):
                        reason = "invalid_json"
                    else:
                        try:
                            state, discarded, degraded_reason = _state_from_loaded_value(value)
                        except JobStoreValidationError as exc:
                            reason = str(exc)
                        else:
                            degraded = bool(degraded_reason)
                            result = self._load_result(
                                state,
                                healthy=not degraded,
                                degraded=degraded,
                                source="primary",
                                reason=degraded_reason,
                                discarded=discarded,
                            )
                            self._last_load_result = result
                            self._last_error_code = degraded_reason
                            return result

            result = self._load_result(
                empty_job_store_state(),
                healthy=False,
                degraded=True,
                source="empty",
                reason=reason,
                discarded=0,
            )
            self._last_load_result = result
            self._last_error_code = reason
            return result

    def _validated_state(
        self,
        jobs: Any,
        next_job_id: Any,
        *,
        media_root: Optional[Path],
    ) -> State:
        if not isinstance(jobs, (list, tuple)) or len(jobs) > MAX_STORED_JOBS:
            raise JobStoreValidationError("invalid_jobs")
        normalized: list[Job] = []
        seen: set[str] = set()
        for raw_job in jobs:
            job = serialize_job(raw_job, media_root=media_root)
            if job["id"] in seen:
                raise JobStoreValidationError("duplicate_job_id")
            seen.add(job["id"])
            normalized.append(job)
        stored_next = _next_job_id(next_job_id)
        if stored_next is None:
            raise JobStoreValidationError("invalid_next_job_id")
        maximum = max((int(job["id"]) for job in normalized), default=0)
        effective_next = max(stored_next, maximum + 1)
        if effective_next > MAX_NEXT_JOB_ID:
            raise JobStoreValidationError("invalid_next_job_id")
        return {
            "version": JOB_STORE_VERSION,
            "next_job_id": effective_next,
            "saved_at": _timestamp_from_clock(self._clock),
            "jobs": apply_retention(normalized),
        }

    def _existing_primary_is_fatally_corrupt(self) -> bool:
        """Return true only when an existing primary cannot be safely parsed."""

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if size > MAX_STORE_BYTES:
            return True
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            _state_from_loaded_value(value)
        except (OSError, TypeError, ValueError, JobStoreValidationError):
            return True
        return False

    @staticmethod
    def _fsync_parent_directory(path: Path) -> None:
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

    def save(
        self,
        jobs: Any,
        next_job_id: Any,
        revision: Any,
        *,
        force: bool = False,
        media_root: Optional[Path] = None,
    ) -> JobStoreSaveResult:
        """Validate and atomically save one snapshot unless its revision is stale."""

        del force  # reserved for P5c throttling; stale revisions are never forced
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision > MAX_REVISION
        ):
            raise JobStoreValidationError("invalid_revision")
        with self._lock:
            if revision < self._last_written_revision:
                state = (
                    deepcopy(self._last_load_result.state)
                    if self._last_load_result is not None
                    else empty_job_store_state()
                )
                return JobStoreSaveResult(
                    state=state, saved=False, stale=True, revision=revision
                )

            state = self._validated_state(
                jobs, next_job_id, media_root=media_root
            )
            text = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
            if len(text.encode("utf-8")) > MAX_STORE_BYTES:
                raise JobStoreValidationError("store_too_large")
            if self._existing_primary_is_fatally_corrupt():
                self._last_error_code = "corrupt_primary_preserved"
                raise JobStorePersistenceError(
                    "Existing corrupt job history was preserved."
                )

            temporary_path: Optional[Path] = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, raw_temporary_path = tempfile.mkstemp(
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                )
                temporary_path = Path(raw_temporary_path)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
                self._fsync_parent_directory(self.path.parent)
            except Exception as exc:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._last_error_code = "persistence_failed"
                raise JobStorePersistenceError(
                    "Could not persist job history."
                ) from exc

            self._last_written_revision = revision
            self._last_save_at = state["saved_at"]
            self._last_error_code = ""
            self._last_load_result = self._load_result(
                state,
                healthy=True,
                degraded=False,
                source="primary",
                reason="",
                discarded=0,
            )
            return JobStoreSaveResult(
                state=deepcopy(state),
                saved=True,
                stale=False,
                revision=revision,
            )

    def status(self) -> Dict[str, Any]:
        """Return safe process-local diagnostics without paths or exceptions."""

        with self._lock:
            loaded = self._last_load_result
            return {
                "healthy": loaded.healthy if loaded is not None else None,
                "degraded": loaded.degraded if loaded is not None else False,
                "source": loaded.source if loaded is not None else "unloaded",
                "reason": loaded.reason if loaded is not None else "",
                "discarded_job_count": (
                    loaded.discarded_job_count if loaded is not None else 0
                ),
                "last_save_at": self._last_save_at,
                "last_error_code": self._last_error_code,
                "last_written_revision": self._last_written_revision,
            }


class UnavailableJobStore:
    """Fail-closed store used when production durability cannot be established."""

    def load(self) -> JobStoreLoadResult:
        raise JobStorePersistenceError("Job history storage is unavailable.")

    def save(self, *args: Any, **kwargs: Any) -> JobStoreSaveResult:
        del args, kwargs
        raise JobStorePersistenceError("Job history storage is unavailable.")

    def status(self) -> Dict[str, Any]:
        return {
            "healthy": False,
            "degraded": True,
            "source": "empty",
            "reason": "store_unavailable",
            "discarded_job_count": 0,
            "last_save_at": None,
            "last_error_code": "persistence_unavailable",
            "last_written_revision": -1,
        }
