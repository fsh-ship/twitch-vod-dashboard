"""Durable identity and retry ownership state for future automatic VOD work."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Callable, Dict, Mapping, Optional

from vod_dashboard.settings import canonical_streamer_login


AUTO_VOD_STATE_FILE_NAME = "auto-vod-state.json"
AUTO_VOD_STATE_VERSION = 1
AUTO_VOD_DISPOSITIONS = frozenset({"pending", "queued", "handled"})
AUTO_VOD_HANDLED_LIMIT_PER_STREAMER = 500
MAX_AUTO_VOD_ATTEMPTS = 1000
MAX_AUTO_VOD_STREAMERS = 1000
MAX_AUTO_VODS_PER_STREAMER = 2000
MAX_AUTO_VOD_RECORDS = 100_000
MAX_AUTO_VOD_ID_LENGTH = 32
MAX_AUTO_VOD_JOB_ID_LENGTH = 20
MAX_AUTO_VOD_REASON_LENGTH = 64
MAX_AUTO_VOD_TIMESTAMP_LENGTH = 40

_VOD_ID_RE = re.compile(rf"\d{{6,{MAX_AUTO_VOD_ID_LENGTH}}}")
_PREFIXED_VOD_ID_RE = re.compile(rf"v(\d{{6,{MAX_AUTO_VOD_ID_LENGTH}}})", re.I)
_JOB_ID_RE = re.compile(rf"[1-9]\d{{0,{MAX_AUTO_VOD_JOB_ID_LENGTH - 1}}}")
_REASON_RE = re.compile(rf"[a-z][a-z0-9_]{{0,{MAX_AUTO_VOD_REASON_LENGTH - 1}}}")

State = Dict[str, Any]
VodRecord = Dict[str, Any]
Clock = Callable[[], datetime]
LogCallback = Callable[[str], None]


class AutoVodStateError(RuntimeError):
    """Base error for the internal Auto VOD state layer."""


class AutoVodStateValidationError(AutoVodStateError, ValueError):
    """Raised when a caller supplies unsafe state transition data."""


class AutoVodStateLoadError(AutoVodStateError):
    """Raised when an existing state file cannot be trusted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AutoVodStatePersistenceError(AutoVodStateError):
    """Raised when an atomic, validated write cannot complete."""


def empty_auto_vod_state() -> State:
    return {"version": AUTO_VOD_STATE_VERSION, "streamers": {}}


def auto_vod_state_path(dashboard_dir: Path) -> Path:
    return Path(dashboard_dir) / AUTO_VOD_STATE_FILE_NAME


def _warn(log: Optional[LogCallback], message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _normalize_vod_id(value: Any, *, accept_prefixed: bool = False) -> str:
    if isinstance(value, bool):
        return ""
    candidate = str(value).strip() if isinstance(value, (str, int)) else ""
    if accept_prefixed:
        prefixed = _PREFIXED_VOD_ID_RE.fullmatch(candidate)
        if prefixed:
            candidate = prefixed.group(1)
    return candidate if _VOD_ID_RE.fullmatch(candidate) else ""


def normalize_auto_vod_id(value: Any) -> str:
    """Normalize a bare or ``v``-prefixed Twitch VOD ID at the API boundary."""
    return _normalize_vod_id(value, accept_prefixed=True)


def _normalize_attempts(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_AUTO_VOD_ATTEMPTS:
        return None
    return value


def _normalize_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _REASON_RE.fullmatch(candidate) else None


def _normalize_job_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _JOB_ID_RE.fullmatch(candidate) else None


def _normalize_utc_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > MAX_AUTO_VOD_TIMESTAMP_LENGTH:
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


def _normalize_record(value: Any) -> Optional[VodRecord]:
    if not isinstance(value, Mapping) or set(value) != {
        "disposition",
        "reason",
        "attempts",
        "retry_after",
        "job_id",
        "discovered_at",
        "updated_at",
    }:
        return None
    disposition = value.get("disposition")
    reason = _normalize_reason(value.get("reason"))
    attempts = _normalize_attempts(value.get("attempts"))
    retry_after = _normalize_utc_timestamp(value.get("retry_after"))
    job_id = _normalize_job_id(value.get("job_id"))
    discovered_at = _normalize_utc_timestamp(value.get("discovered_at"))
    updated_at = _normalize_utc_timestamp(value.get("updated_at"))
    if (
        disposition not in AUTO_VOD_DISPOSITIONS
        or attempts is None
        or discovered_at is None
        or updated_at is None
        or (value.get("reason") is not None and reason is None)
        or (value.get("retry_after") is not None and retry_after is None)
        or (value.get("job_id") is not None and job_id is None)
    ):
        return None
    if disposition == "pending" and job_id is not None:
        return None
    if disposition == "queued" and (job_id is None or reason is not None or retry_after is not None):
        return None
    if disposition == "handled" and (reason is None or retry_after is not None):
        return None
    return {
        "disposition": disposition,
        "reason": reason,
        "attempts": attempts,
        "retry_after": retry_after,
        "job_id": job_id,
        "discovered_at": discovered_at,
        "updated_at": updated_at,
    }


def normalize_auto_vod_state(value: Any) -> State:
    """Strictly validate complete persisted state; any bad record fails closed."""
    if not isinstance(value, Mapping) or set(value) != {"version", "streamers"}:
        raise AutoVodStateLoadError("invalid_structure")
    version = value.get("version")
    if isinstance(version, bool) or version != AUTO_VOD_STATE_VERSION:
        raise AutoVodStateLoadError("unsupported_version")
    raw_streamers = value.get("streamers")
    if not isinstance(raw_streamers, Mapping) or len(raw_streamers) > MAX_AUTO_VOD_STREAMERS:
        raise AutoVodStateLoadError("invalid_structure")

    streamers: Dict[str, Dict[str, Dict[str, VodRecord]]] = {}
    total_records = 0
    for raw_streamer, raw_bucket in raw_streamers.items():
        streamer = canonical_streamer_login(raw_streamer)
        if not streamer or streamer != raw_streamer:
            raise AutoVodStateLoadError("invalid_record")
        if not isinstance(raw_bucket, Mapping) or set(raw_bucket) != {"vods"}:
            raise AutoVodStateLoadError("invalid_record")
        raw_vods = raw_bucket.get("vods")
        if not isinstance(raw_vods, Mapping) or len(raw_vods) > MAX_AUTO_VODS_PER_STREAMER:
            raise AutoVodStateLoadError("invalid_record")
        vods: Dict[str, VodRecord] = {}
        for raw_vod_id, raw_record in raw_vods.items():
            vod_id = _normalize_vod_id(raw_vod_id)
            record = _normalize_record(raw_record)
            if not vod_id or vod_id != raw_vod_id or record is None:
                raise AutoVodStateLoadError("invalid_record")
            vods[vod_id] = record
            total_records += 1
            if total_records > MAX_AUTO_VOD_RECORDS:
                raise AutoVodStateLoadError("invalid_record")
        streamers[streamer] = {"vods": vods}
    return {"version": AUTO_VOD_STATE_VERSION, "streamers": streamers}


def _required_streamer(value: Any) -> str:
    streamer = canonical_streamer_login(value)
    if not streamer:
        raise AutoVodStateValidationError("invalid_streamer")
    return streamer


def _required_vod_id(value: Any) -> str:
    vod_id = normalize_auto_vod_id(value)
    if not vod_id:
        raise AutoVodStateValidationError("invalid_vod_id")
    return vod_id


def _required_attempts(value: Any) -> int:
    attempts = _normalize_attempts(value)
    if attempts is None:
        raise AutoVodStateValidationError("invalid_attempts")
    return attempts


def _required_reason(value: Any) -> str:
    reason = _normalize_reason(value)
    if reason is None:
        raise AutoVodStateValidationError("invalid_reason")
    return reason


def _required_job_id(value: Any) -> str:
    job_id = _normalize_job_id(value)
    if job_id is None:
        raise AutoVodStateValidationError("invalid_job_id")
    return job_id


def _optional_retry_after(value: Any) -> Optional[str]:
    timestamp = _normalize_utc_timestamp(value)
    if value is not None and timestamp is None:
        raise AutoVodStateValidationError("invalid_retry_after")
    return timestamp


def _timestamp_from_clock(clock: Clock) -> str:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise AutoVodStateValidationError("invalid_clock")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def apply_auto_vod_retention(state: Any) -> State:
    """Prune only deterministic oldest handled records, per streamer."""
    normalized = normalize_auto_vod_state(state)
    retained = deepcopy(normalized)
    for bucket in retained["streamers"].values():
        vods = bucket["vods"]
        handled = [
            (vod_id, record)
            for vod_id, record in vods.items()
            if record["disposition"] == "handled"
        ]
        handled.sort(
            key=lambda item: (
                item[1]["updated_at"],
                item[1]["discovered_at"],
                item[0],
            )
        )
        for vod_id, _record in handled[:-AUTO_VOD_HANDLED_LIMIT_PER_STREAMER]:
            del vods[vod_id]
    return retained


class AutoVodStateStore:
    """Thread-safe, atomic persistence for future Auto VOD ownership only."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Optional[Clock] = None,
        log: Optional[LogCallback] = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._log = log
        self._lock = threading.RLock()

    @classmethod
    def from_dashboard_dir(
        cls,
        dashboard_dir: Path,
        *,
        clock: Optional[Clock] = None,
        log: Optional[LogCallback] = None,
    ) -> "AutoVodStateStore":
        return cls(auto_vod_state_path(dashboard_dir), clock=clock, log=log)

    def _load_locked(self) -> State:
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return empty_auto_vod_state()
        except Exception as exc:
            _warn(self._log, "Auto VOD state could not be read.")
            raise AutoVodStateLoadError("unreadable_state") from exc
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            _warn(self._log, "Auto VOD state is invalid JSON.")
            raise AutoVodStateLoadError("invalid_json") from exc
        return normalize_auto_vod_state(value)

    def load(self) -> State:
        with self._lock:
            return self._load_locked()

    def snapshot(self) -> State:
        return self.load()

    def get_vod(self, streamer: Any, vod_id: Any) -> Optional[VodRecord]:
        canonical_streamer = canonical_streamer_login(streamer)
        canonical_vod_id = normalize_auto_vod_id(vod_id)
        if not canonical_streamer or not canonical_vod_id:
            return None
        with self._lock:
            record = self._load_locked()["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            return deepcopy(record) if record is not None else None

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

    def _write_locked(self, state: State) -> None:
        try:
            normalized = apply_auto_vod_retention(state)
        except AutoVodStateLoadError as exc:
            raise AutoVodStateValidationError("invalid_state") from exc
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
                json.dump(normalized, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            self._fsync_parent_directory(self.path.parent)
        except AutoVodStateValidationError:
            raise
        except Exception as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise AutoVodStatePersistenceError(
                "Could not persist Auto VOD state."
            ) from exc

    def _persist_record_locked(
        self, state: State, streamer: str, vod_id: str, record: VodRecord
    ) -> VodRecord:
        state["streamers"].setdefault(streamer, {"vods": {}})["vods"][vod_id] = record
        self._write_locked(state)
        return deepcopy(record)

    def ensure_pending(
        self, streamer: Any, vod_id: Any, *, reason: Any = "new_vod"
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        pending_reason = _required_reason(reason)
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            if existing is not None:
                return deepcopy(existing)
            now = _timestamp_from_clock(self._clock)
            return self._persist_record_locked(
                state,
                canonical_streamer,
                canonical_vod_id,
                {
                    "disposition": "pending",
                    "reason": pending_reason,
                    "attempts": 0,
                    "retry_after": None,
                    "job_id": None,
                    "discovered_at": now,
                    "updated_at": now,
                },
            )

    def set_pending(
        self,
        streamer: Any,
        vod_id: Any,
        *,
        reason: Any,
        attempts: int,
        retry_after: Optional[str] = None,
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        pending_reason = _required_reason(reason)
        normalized_attempts = _required_attempts(attempts)
        normalized_retry_after = _optional_retry_after(retry_after)
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            if existing is None:
                now = _timestamp_from_clock(self._clock)
                discovered_at = now
                updated_at = now
            else:
                if existing["disposition"] == "handled":
                    return deepcopy(existing)
                if existing["disposition"] == "queued":
                    raise AutoVodStateValidationError("invalid_transition")
                discovered_at = existing["discovered_at"]
                candidate = {
                    "disposition": "pending",
                    "reason": pending_reason,
                    "attempts": normalized_attempts,
                    "retry_after": normalized_retry_after,
                    "job_id": None,
                    "discovered_at": discovered_at,
                    "updated_at": existing["updated_at"],
                }
                if candidate == existing:
                    return deepcopy(existing)
                updated_at = _timestamp_from_clock(self._clock)
            record = {
                "disposition": "pending",
                "reason": pending_reason,
                "attempts": normalized_attempts,
                "retry_after": normalized_retry_after,
                "job_id": None,
                "discovered_at": discovered_at,
                "updated_at": updated_at,
            }
            return self._persist_record_locked(
                state, canonical_streamer, canonical_vod_id, record
            )

    def set_queued(self, streamer: Any, vod_id: Any, job_id: Any) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        normalized_job_id = _required_job_id(job_id)
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            if existing is None:
                raise AutoVodStateValidationError("vod_not_found")
            if existing["disposition"] == "handled":
                return deepcopy(existing)
            if existing["disposition"] == "queued":
                if existing["job_id"] == normalized_job_id:
                    return deepcopy(existing)
                raise AutoVodStateValidationError("job_ownership_conflict")
            record = {
                "disposition": "queued",
                "reason": None,
                "attempts": existing["attempts"],
                "retry_after": None,
                "job_id": normalized_job_id,
                "discovered_at": existing["discovered_at"],
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_record_locked(
                state, canonical_streamer, canonical_vod_id, record
            )

    def update_retry(
        self,
        streamer: Any,
        vod_id: Any,
        *,
        attempts: int,
        retry_after: Optional[str],
        reason: Any,
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        normalized_attempts = _required_attempts(attempts)
        normalized_retry_after = _optional_retry_after(retry_after)
        pending_reason = _required_reason(reason)
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            if existing is None:
                raise AutoVodStateValidationError("vod_not_found")
            if existing["disposition"] == "handled":
                return deepcopy(existing)
            if existing["disposition"] != "queued":
                raise AutoVodStateValidationError("invalid_transition")
            record = {
                "disposition": "pending",
                "reason": pending_reason,
                "attempts": normalized_attempts,
                "retry_after": normalized_retry_after,
                "job_id": None,
                "discovered_at": existing["discovered_at"],
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_record_locked(
                state, canonical_streamer, canonical_vod_id, record
            )

    def set_handled(
        self,
        streamer: Any,
        vod_id: Any,
        *,
        reason: Any,
        job_id: Optional[Any] = None,
        attempts: Optional[int] = None,
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        handled_reason = _required_reason(reason)
        normalized_job_id = (
            _required_job_id(job_id) if job_id is not None else None
        )
        normalized_attempts = (
            _required_attempts(attempts) if attempts is not None else None
        )
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, {"vods": {}}
            )["vods"].get(canonical_vod_id)
            if existing is None:
                raise AutoVodStateValidationError("vod_not_found")
            if existing["disposition"] == "handled":
                return deepcopy(existing)
            record = {
                "disposition": "handled",
                "reason": handled_reason,
                "attempts": (
                    normalized_attempts
                    if normalized_attempts is not None
                    else existing["attempts"]
                ),
                "retry_after": None,
                "job_id": (
                    normalized_job_id
                    if normalized_job_id is not None
                    else existing["job_id"]
                ),
                "discovered_at": existing["discovered_at"],
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_record_locked(
                state, canonical_streamer, canonical_vod_id, record
            )
