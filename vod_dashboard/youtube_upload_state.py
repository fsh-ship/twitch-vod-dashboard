"""Durable ownership ledger for future automatic YouTube uploads.

This module deliberately has no Flask, JobStore, media-I/O, or YouTube API
dependency.  It records the single canonical ownership identity that later
automation slices must claim before attempting an upload.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from vod_dashboard.runtime_files import atomic_write_text
from vod_dashboard.settings import canonical_streamer_login


YOUTUBE_UPLOAD_STATE_FILE_NAME = "youtube-upload-state.json"
YOUTUBE_UPLOAD_STATE_VERSION = 1
MAX_UPLOAD_RECORDS = 100_000
MAX_VOD_ID_LENGTH = 32
MAX_PATH_LENGTH = 1024
MAX_IDENTIFIER_LENGTH = 128
MAX_REASON_LENGTH = 64
MAX_TIMESTAMP_LENGTH = 40
MAX_SIZE_BYTES = 2**63 - 1

UPLOAD_STATES = frozenset({
    "intent_pending", "upload_queued", "transfer_started", "video_confirmed",
    "playlist_pending", "completed", "blocked_youtube", "needs_attention",
    "cancelled",
})
PLAYLIST_STATES = frozenset({
    "not_requested", "pending", "inserting", "confirmed", "failed", "uncertain",
})
REASON_CODES = frozenset({
    "youtube_not_connected", "token_refresh_failed", "api_unavailable",
    "local_preparation_failed", "upload_outcome_uncertain", "playlist_failed",
    "playlist_uncertain",
})

_VOD_ID_RE = re.compile(rf"\d{{6,{MAX_VOD_ID_LENGTH}}}")
_IDENTIFIER_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.-]{{0,{MAX_IDENTIFIER_LENGTH - 1}}}")
_YOUTUBE_ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_IDENTIFIER_LENGTH}}}")
_TIMESTAMP_RE = re.compile(r".+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_STATE_TRANSITIONS = {
    "intent_pending": {"upload_queued", "blocked_youtube", "needs_attention", "cancelled"},
    "upload_queued": {"transfer_started", "blocked_youtube", "needs_attention", "cancelled"},
    "transfer_started": {"video_confirmed", "blocked_youtube", "needs_attention"},
    "video_confirmed": {"playlist_pending", "completed", "needs_attention"},
    "playlist_pending": {"completed", "needs_attention"},
    "blocked_youtube": {"upload_queued", "cancelled", "needs_attention"},
    "needs_attention": {"upload_queued", "cancelled"},
    "cancelled": {"upload_queued"},
    "completed": set(),
}
_PLAYLIST_TRANSITIONS = {
    "not_requested": {"pending"},
    "pending": {"inserting", "failed", "uncertain"},
    "inserting": {"confirmed", "failed", "uncertain"},
    "failed": {"pending"},
    "uncertain": {"pending"},
    "confirmed": set(),
}

State = Dict[str, Any]
UploadRecord = Dict[str, Any]
Clock = Callable[[], datetime]


class YouTubeUploadStateError(RuntimeError):
    """Base error for the Auto YouTube ownership state layer."""


class YouTubeUploadStateValidationError(YouTubeUploadStateError, ValueError):
    """Raised for invalid identity, record, or requested transition data."""


class YouTubeUploadStateLoadError(YouTubeUploadStateError):
    """Raised when an existing ownership file cannot be trusted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class YouTubeUploadStatePersistenceError(YouTubeUploadStateError):
    """Raised when an atomic ownership-state write cannot complete."""


def empty_youtube_upload_state() -> State:
    return {"version": YOUTUBE_UPLOAD_STATE_VERSION, "uploads": {}}


def youtube_upload_state_path(dashboard_dir: Path) -> Path:
    return Path(dashboard_dir) / YOUTUBE_UPLOAD_STATE_FILE_NAME


def _required_streamer(value: Any) -> str:
    streamer = canonical_streamer_login(value)
    if not streamer:
        raise YouTubeUploadStateValidationError("invalid_streamer")
    return streamer


def _required_vod_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise YouTubeUploadStateValidationError("invalid_twitch_vod_id")
    vod_id = str(value).strip()
    if not _VOD_ID_RE.fullmatch(vod_id):
        raise YouTubeUploadStateValidationError("invalid_twitch_vod_id")
    return vod_id


def canonical_upload_key(streamer: Any, twitch_vod_id: Any) -> str:
    """Return the durable ``streamer:numeric-vod-id`` ownership key."""
    return f"{_required_streamer(streamer)}:{_required_vod_id(twitch_vod_id)}"


def _required_identifier(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise YouTubeUploadStateValidationError(code)
    candidate = value.strip()
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise YouTubeUploadStateValidationError(code)
    return candidate


def _optional_youtube_id(value: Any, code: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise YouTubeUploadStateValidationError(code)
    candidate = value.strip()
    if not _YOUTUBE_ID_RE.fullmatch(candidate):
        raise YouTubeUploadStateValidationError(code)
    return candidate


def _required_relative_media_path(value: Any) -> str:
    if not isinstance(value, str):
        raise YouTubeUploadStateValidationError("invalid_media_path")
    candidate = value.strip().replace("\\", "/")
    if (
        not candidate or len(candidate) > MAX_PATH_LENGTH or _CONTROL_RE.search(candidate)
        or "://" in candidate or candidate.startswith("//")
        or re.match(r"^[A-Za-z]:", candidate)
    ):
        raise YouTubeUploadStateValidationError("invalid_media_path")
    path = PurePosixPath(candidate)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise YouTubeUploadStateValidationError("invalid_media_path")
    return path.as_posix()


def _required_size_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SIZE_BYTES:
        raise YouTubeUploadStateValidationError("invalid_size_bytes")
    return value


def _optional_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise YouTubeUploadStateValidationError("invalid_reason")
    reason = value.strip()
    if len(reason) > MAX_REASON_LENGTH or reason not in REASON_CODES:
        raise YouTubeUploadStateValidationError("invalid_reason")
    return reason


def _required_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_LENGTH or not _TIMESTAMP_RE.fullmatch(value):
        raise YouTubeUploadStateValidationError("invalid_timestamp")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate)
    except ValueError as exc:
        raise YouTubeUploadStateValidationError("invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise YouTubeUploadStateValidationError("invalid_timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_attempts(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise YouTubeUploadStateValidationError("invalid_attempts")
    return value


def _normalize_record(value: Any, *, key: str) -> UploadRecord:
    required_fields = {
        "streamer", "twitch_vod_id", "source_download_job_id", "source_download_item_id",
        "media_path", "size_bytes", "state", "upload_job_id", "attempts",
        "youtube_video_id", "playlist_id", "playlist_state", "reason", "created_at", "updated_at",
    }
    if not isinstance(value, Mapping) or set(value) != required_fields:
        raise YouTubeUploadStateLoadError("invalid_record")
    try:
        streamer = _required_streamer(value.get("streamer"))
        vod_id = _required_vod_id(value.get("twitch_vod_id"))
        if key != f"{streamer}:{vod_id}":
            raise YouTubeUploadStateValidationError("invalid_record")
        state = value.get("state")
        playlist_state = value.get("playlist_state")
        if state not in UPLOAD_STATES or playlist_state not in PLAYLIST_STATES:
            raise YouTubeUploadStateValidationError("invalid_record")
        upload_job_id = value.get("upload_job_id")
        if upload_job_id is not None:
            upload_job_id = _required_identifier(upload_job_id, "invalid_upload_job_id")
        playlist_id = _optional_youtube_id(value.get("playlist_id"), "invalid_playlist_id")
        youtube_video_id = _optional_youtube_id(value.get("youtube_video_id"), "invalid_youtube_video_id")
        if playlist_state == "not_requested" and playlist_id is not None:
            raise YouTubeUploadStateValidationError("invalid_record")
        if playlist_state != "not_requested" and playlist_id is None:
            raise YouTubeUploadStateValidationError("invalid_record")
        if state in {"upload_queued", "transfer_started"} and upload_job_id is None:
            raise YouTubeUploadStateValidationError("invalid_record")
        if state in {"video_confirmed", "playlist_pending", "completed"} and youtube_video_id is None:
            raise YouTubeUploadStateValidationError("invalid_record")
        return {
            "streamer": streamer,
            "twitch_vod_id": vod_id,
            "source_download_job_id": _required_identifier(value.get("source_download_job_id"), "invalid_source_download_job_id"),
            "source_download_item_id": _required_identifier(value.get("source_download_item_id"), "invalid_source_download_item_id"),
            "media_path": _required_relative_media_path(value.get("media_path")),
            "size_bytes": _required_size_bytes(value.get("size_bytes")),
            "state": state,
            "upload_job_id": upload_job_id,
            "attempts": _required_attempts(value.get("attempts")),
            "youtube_video_id": youtube_video_id,
            "playlist_id": playlist_id,
            "playlist_state": playlist_state,
            "reason": _optional_reason(value.get("reason")),
            "created_at": _required_timestamp(value.get("created_at")),
            "updated_at": _required_timestamp(value.get("updated_at")),
        }
    except YouTubeUploadStateValidationError as exc:
        raise YouTubeUploadStateLoadError("invalid_record") from exc


def normalize_youtube_upload_state(value: Any) -> State:
    """Strictly validate the complete version-1 ownership document."""
    if not isinstance(value, Mapping) or set(value) != {"version", "uploads"}:
        raise YouTubeUploadStateLoadError("invalid_structure")
    if isinstance(value.get("version"), bool) or value.get("version") != YOUTUBE_UPLOAD_STATE_VERSION:
        raise YouTubeUploadStateLoadError("unsupported_version")
    uploads = value.get("uploads")
    if not isinstance(uploads, Mapping) or len(uploads) > MAX_UPLOAD_RECORDS:
        raise YouTubeUploadStateLoadError("invalid_structure")
    normalized: Dict[str, UploadRecord] = {}
    for key, record in uploads.items():
        if not isinstance(key, str) or key in normalized:
            raise YouTubeUploadStateLoadError("invalid_record")
        normalized[key] = _normalize_record(record, key=key)
    return {"version": YOUTUBE_UPLOAD_STATE_VERSION, "uploads": normalized}


def _timestamp_from_clock(clock: Clock) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise YouTubeUploadStateValidationError("invalid_clock")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class YouTubeUploadStateStore:
    """Process-local-lock, atomic persistence for Auto YouTube ownership."""

    def __init__(self, path: Path, *, clock: Optional[Clock] = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    @classmethod
    def from_dashboard_dir(cls, dashboard_dir: Path, *, clock: Optional[Clock] = None) -> "YouTubeUploadStateStore":
        return cls(youtube_upload_state_path(dashboard_dir), clock=clock)

    def _load_locked(self) -> State:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_youtube_upload_state()
        except Exception as exc:
            raise YouTubeUploadStateLoadError("unreadable_state") from exc
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise YouTubeUploadStateLoadError("invalid_json") from exc
        return normalize_youtube_upload_state(value)

    def load(self) -> State:
        with self._lock:
            return deepcopy(self._load_locked())

    snapshot = load

    def health(self) -> Dict[str, Any]:
        """Return safe health data without paths, content, or exceptions."""
        try:
            self.load()
        except YouTubeUploadStateLoadError as exc:
            return {"healthy": False, "reason": exc.reason}
        return {"healthy": True, "reason": None}

    def get(self, streamer: Any, twitch_vod_id: Any) -> Optional[UploadRecord]:
        try:
            key = canonical_upload_key(streamer, twitch_vod_id)
        except YouTubeUploadStateValidationError:
            return None
        with self._lock:
            record = self._load_locked()["uploads"].get(key)
            return deepcopy(record) if record is not None else None

    def list_records(self) -> Dict[str, UploadRecord]:
        with self._lock:
            return deepcopy(self._load_locked()["uploads"])

    def _write_locked(self, state: State) -> None:
        try:
            normalized = normalize_youtube_upload_state(state)
            text = json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            atomic_write_text(self.path, text, encoding="utf-8")
        except YouTubeUploadStateLoadError as exc:
            raise YouTubeUploadStateValidationError("invalid_state") from exc
        except YouTubeUploadStateValidationError:
            raise
        except Exception as exc:
            raise YouTubeUploadStatePersistenceError("Could not persist Auto YouTube ownership state.") from exc

    def create_intent_if_absent(
        self,
        streamer: Any,
        twitch_vod_id: Any,
        *,
        source_download_job_id: Any,
        source_download_item_id: Any,
        media_path: Any,
        size_bytes: Any,
        playlist_id: Any = None,
    ) -> Tuple[UploadRecord, bool]:
        """Durably claim one VOD once; later calls return its untouched owner."""
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(twitch_vod_id)
        key = f"{canonical_streamer}:{canonical_vod_id}"
        candidate = {
            "streamer": canonical_streamer,
            "twitch_vod_id": canonical_vod_id,
            "source_download_job_id": _required_identifier(source_download_job_id, "invalid_source_download_job_id"),
            "source_download_item_id": _required_identifier(source_download_item_id, "invalid_source_download_item_id"),
            "media_path": _required_relative_media_path(media_path),
            "size_bytes": _required_size_bytes(size_bytes),
        }
        frozen_playlist_id = _optional_youtube_id(
            playlist_id, "invalid_playlist_id"
        )
        with self._lock:
            state = self._load_locked()
            existing = state["uploads"].get(key)
            if existing is not None:
                return deepcopy(existing), False
            if len(state["uploads"]) >= MAX_UPLOAD_RECORDS:
                raise YouTubeUploadStateValidationError("too_many_uploads")
            now = _timestamp_from_clock(self._clock)
            record: UploadRecord = {
                **candidate,
                "state": "intent_pending",
                "upload_job_id": None,
                "attempts": 0,
                "youtube_video_id": None,
                "playlist_id": frozen_playlist_id,
                "playlist_state": (
                    "pending" if frozen_playlist_id is not None else "not_requested"
                ),
                "reason": None,
                "created_at": now,
                "updated_at": now,
            }
            state["uploads"][key] = record
            self._write_locked(state)
            return deepcopy(record), True

    def update_record(
        self,
        streamer: Any,
        twitch_vod_id: Any,
        *,
        state: Optional[str] = None,
        upload_job_id: Any = ...,
        youtube_video_id: Any = ...,
        playlist_id: Any = ...,
        playlist_state: Optional[str] = None,
        reason: Any = ...,
        attempts: Optional[int] = None,
    ) -> UploadRecord:
        """Persist a validated transition without permitting identity mutation."""
        key = canonical_upload_key(streamer, twitch_vod_id)
        with self._lock:
            document = self._load_locked()
            existing = document["uploads"].get(key)
            if existing is None:
                raise YouTubeUploadStateValidationError("upload_not_found")
            candidate = deepcopy(existing)
            if state is not None:
                if state not in UPLOAD_STATES:
                    raise YouTubeUploadStateValidationError("invalid_state")
                if state != existing["state"] and state not in _STATE_TRANSITIONS[existing["state"]]:
                    raise YouTubeUploadStateValidationError("invalid_transition")
                candidate["state"] = state
            if playlist_state is not None:
                if playlist_state not in PLAYLIST_STATES:
                    raise YouTubeUploadStateValidationError("invalid_playlist_state")
                if playlist_state != existing["playlist_state"] and playlist_state not in _PLAYLIST_TRANSITIONS[existing["playlist_state"]]:
                    raise YouTubeUploadStateValidationError("invalid_playlist_transition")
                candidate["playlist_state"] = playlist_state
            if upload_job_id is not ...:
                candidate["upload_job_id"] = (
                    None if upload_job_id is None else _required_identifier(upload_job_id, "invalid_upload_job_id")
                )
            if youtube_video_id is not ...:
                video_id = _optional_youtube_id(youtube_video_id, "invalid_youtube_video_id")
                if existing["youtube_video_id"] is not None and video_id != existing["youtube_video_id"]:
                    raise YouTubeUploadStateValidationError("youtube_video_id_immutable")
                candidate["youtube_video_id"] = video_id
            if playlist_id is not ...:
                candidate["playlist_id"] = _optional_youtube_id(playlist_id, "invalid_playlist_id")
            if reason is not ...:
                candidate["reason"] = _optional_reason(reason)
            if attempts is not None:
                candidate["attempts"] = _required_attempts(attempts)
                if candidate["attempts"] < existing["attempts"]:
                    raise YouTubeUploadStateValidationError("invalid_attempts")
            candidate["updated_at"] = _timestamp_from_clock(self._clock)
            try:
                normalized = _normalize_record(candidate, key=key)
            except YouTubeUploadStateLoadError as exc:
                raise YouTubeUploadStateValidationError("invalid_record") from exc
            if normalized == existing:
                return deepcopy(existing)
            document["uploads"][key] = normalized
            self._write_locked(document)
            return deepcopy(normalized)
