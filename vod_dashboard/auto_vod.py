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
AUTO_VOD_STATE_VERSION = 2
LEGACY_AUTO_VOD_STATE_VERSION = 1
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


class AutoVodStateMigrationRequired(AutoVodStateLoadError):
    """Raised for a valid legacy v1 state which must not be scheduled."""

    def __init__(self) -> None:
        super().__init__("migration_required")


class AutoVodStatePersistenceError(AutoVodStateError):
    """Raised when an atomic, validated write cannot complete."""


def empty_auto_vod_state() -> State:
    return {"version": AUTO_VOD_STATE_VERSION, "streamers": {}}


def _empty_streamer_bucket() -> Dict[str, Any]:
    return {
        "baseline_initialized": False,
        "baseline_established_at": None,
        "vods": {},
    }


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


def _normalize_vods(raw_vods: Any) -> Dict[str, VodRecord]:
    if not isinstance(raw_vods, Mapping) or len(raw_vods) > MAX_AUTO_VODS_PER_STREAMER:
        raise AutoVodStateLoadError("invalid_record")
    vods: Dict[str, VodRecord] = {}
    for raw_vod_id, raw_record in raw_vods.items():
        vod_id = _normalize_vod_id(raw_vod_id)
        record = _normalize_record(raw_record)
        if not vod_id or vod_id != raw_vod_id or record is None:
            raise AutoVodStateLoadError("invalid_record")
        vods[vod_id] = record
    return vods


def normalize_legacy_auto_vod_state(value: Any) -> State:
    """Strictly validate v1 for an explicit offline migration only."""
    if not isinstance(value, Mapping) or set(value) != {"version", "streamers"}:
        raise AutoVodStateLoadError("invalid_structure")
    version = value.get("version")
    if isinstance(version, bool) or version != LEGACY_AUTO_VOD_STATE_VERSION:
        raise AutoVodStateLoadError("unsupported_version")
    raw_streamers = value.get("streamers")
    if not isinstance(raw_streamers, Mapping) or len(raw_streamers) > MAX_AUTO_VOD_STREAMERS:
        raise AutoVodStateLoadError("invalid_structure")
    streamers: Dict[str, Dict[str, Any]] = {}
    total_records = 0
    for raw_streamer, raw_bucket in raw_streamers.items():
        streamer = canonical_streamer_login(raw_streamer)
        if not streamer or streamer != raw_streamer:
            raise AutoVodStateLoadError("invalid_record")
        if not isinstance(raw_bucket, Mapping) or set(raw_bucket) != {"vods"}:
            raise AutoVodStateLoadError("invalid_record")
        vods = _normalize_vods(raw_bucket.get("vods"))
        total_records += len(vods)
        if total_records > MAX_AUTO_VOD_RECORDS:
            raise AutoVodStateLoadError("invalid_record")
        streamers[streamer] = {"vods": vods}
    return {"version": LEGACY_AUTO_VOD_STATE_VERSION, "streamers": streamers}


def normalize_auto_vod_state(value: Any) -> State:
    """Strictly validate v2 state; recognized v1 state fails closed for migration."""
    if not isinstance(value, Mapping) or set(value) != {"version", "streamers"}:
        raise AutoVodStateLoadError("invalid_structure")
    version = value.get("version")
    if isinstance(version, bool):
        raise AutoVodStateLoadError("unsupported_version")
    if version == LEGACY_AUTO_VOD_STATE_VERSION:
        normalize_legacy_auto_vod_state(value)
        raise AutoVodStateMigrationRequired()
    if version != AUTO_VOD_STATE_VERSION:
        raise AutoVodStateLoadError("unsupported_version")
    raw_streamers = value.get("streamers")
    if not isinstance(raw_streamers, Mapping) or len(raw_streamers) > MAX_AUTO_VOD_STREAMERS:
        raise AutoVodStateLoadError("invalid_structure")

    streamers: Dict[str, Dict[str, Any]] = {}
    total_records = 0
    for raw_streamer, raw_bucket in raw_streamers.items():
        streamer = canonical_streamer_login(raw_streamer)
        if not streamer or streamer != raw_streamer:
            raise AutoVodStateLoadError("invalid_record")
        if not isinstance(raw_bucket, Mapping) or set(raw_bucket) != {
            "baseline_initialized",
            "baseline_established_at",
            "vods",
        }:
            raise AutoVodStateLoadError("invalid_record")
        baseline_initialized = raw_bucket.get("baseline_initialized")
        baseline_established_at = _normalize_utc_timestamp(
            raw_bucket.get("baseline_established_at")
        )
        if (
            not isinstance(baseline_initialized, bool)
            or (
                baseline_initialized
                and baseline_established_at is None
            )
            or (
                not baseline_initialized
                and raw_bucket.get("baseline_established_at") is not None
            )
        ):
            raise AutoVodStateLoadError("invalid_record")
        vods = _normalize_vods(raw_bucket.get("vods"))
        total_records += len(vods)
        if total_records > MAX_AUTO_VOD_RECORDS:
            raise AutoVodStateLoadError("invalid_record")
        streamers[streamer] = {
            "baseline_initialized": baseline_initialized,
            "baseline_established_at": baseline_established_at,
            "vods": vods,
        }
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
    """Prune only deterministic non-baseline handled records, per streamer."""
    normalized = normalize_auto_vod_state(state)
    retained = deepcopy(normalized)
    for bucket in retained["streamers"].values():
        vods = bucket["vods"]
        handled = [
            (vod_id, record)
            for vod_id, record in vods.items()
            if (
                record["disposition"] == "handled"
                and record["reason"] != "baseline_existing"
            )
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
                canonical_streamer, _empty_streamer_bucket()
            )["vods"].get(canonical_vod_id)
            return deepcopy(record) if record is not None else None

    def baseline_initialized(self, streamer: Any) -> bool:
        """Return whether this streamer's initial successful discovery is durable."""
        canonical_streamer = canonical_streamer_login(streamer)
        if not canonical_streamer:
            return False
        with self._lock:
            bucket = self._load_locked()["streamers"].get(
                canonical_streamer, _empty_streamer_bucket()
            )
            return bucket["baseline_initialized"] is True

    def baseline_existing_vod_ids(self) -> Dict[str, set[str]]:
        """Return only the durable identities intentionally handled at baseline."""
        with self._lock:
            state = self._load_locked()
            return {
                streamer: {
                    vod_id
                    for vod_id, record in bucket["vods"].items()
                    if (
                        record["disposition"] == "handled"
                        and record["reason"] == "baseline_existing"
                    )
                }
                for streamer, bucket in state["streamers"].items()
                if any(
                    record["disposition"] == "handled"
                    and record["reason"] == "baseline_existing"
                    for record in bucket["vods"].values()
                )
            }

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

    def _write_locked(self, state: State, *, apply_retention: bool = True) -> None:
        try:
            normalized = (
                apply_auto_vod_retention(state)
                if apply_retention
                else normalize_auto_vod_state(state)
            )
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
        state["streamers"].setdefault(streamer, _empty_streamer_bucket())["vods"][
            vod_id
        ] = record
        self._write_locked(state)
        return deepcopy(record)

    def replace_state(self, state: Any, *, apply_retention: bool = True) -> State:
        """Atomically replace a complete validated v2 state for an offline tool."""
        with self._lock:
            normalized = normalize_auto_vod_state(state)
            self._write_locked(normalized, apply_retention=apply_retention)
            return deepcopy(normalized)

    def establish_baseline(self, streamer: Any, vod_ids: Any) -> Dict[str, Any]:
        """Atomically mark a streamer's first successful discovery as existing work."""
        canonical_streamer = _required_streamer(streamer)
        if isinstance(vod_ids, (str, bytes, Mapping)):
            raise AutoVodStateValidationError("invalid_vod_ids")
        try:
            raw_vod_ids = list(vod_ids)
        except TypeError as exc:
            raise AutoVodStateValidationError("invalid_vod_ids") from exc
        canonical_vod_ids: list[str] = []
        seen: set[str] = set()
        for value in raw_vod_ids:
            vod_id = _required_vod_id(value)
            if vod_id not in seen:
                seen.add(vod_id)
                canonical_vod_ids.append(vod_id)

        with self._lock:
            state = self._load_locked()
            bucket = state["streamers"].setdefault(
                canonical_streamer, _empty_streamer_bucket()
            )
            if bucket["baseline_initialized"] is True:
                return deepcopy(bucket)
            if len(bucket["vods"]) + len(
                [vod_id for vod_id in canonical_vod_ids if vod_id not in bucket["vods"]]
            ) > MAX_AUTO_VODS_PER_STREAMER:
                raise AutoVodStateValidationError("too_many_vods")
            now = _timestamp_from_clock(self._clock)
            bucket["baseline_initialized"] = True
            bucket["baseline_established_at"] = now
            for vod_id in canonical_vod_ids:
                existing = bucket["vods"].get(vod_id)
                if existing is not None and existing["disposition"] == "handled":
                    continue
                bucket["vods"][vod_id] = {
                    "disposition": "handled",
                    "reason": "baseline_existing",
                    "attempts": 0,
                    "retry_after": None,
                    "job_id": None,
                    "discovered_at": now,
                    "updated_at": now,
                }
            self._write_locked(state)
            return deepcopy(bucket)

    def ensure_pending(
        self, streamer: Any, vod_id: Any, *, reason: Any = "new_vod"
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        pending_reason = _required_reason(reason)
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, _empty_streamer_bucket()
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
                canonical_streamer, _empty_streamer_bucket()
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

    def set_queued(
        self,
        streamer: Any,
        vod_id: Any,
        job_id: Any,
        *,
        attempts: Optional[int] = None,
    ) -> VodRecord:
        canonical_streamer = _required_streamer(streamer)
        canonical_vod_id = _required_vod_id(vod_id)
        normalized_job_id = _required_job_id(job_id)
        normalized_attempts = (
            _required_attempts(attempts) if attempts is not None else None
        )
        with self._lock:
            state = self._load_locked()
            existing = state["streamers"].get(
                canonical_streamer, _empty_streamer_bucket()
            )["vods"].get(canonical_vod_id)
            if existing is None:
                raise AutoVodStateValidationError("vod_not_found")
            if existing["disposition"] == "handled":
                return deepcopy(existing)
            if existing["disposition"] == "queued":
                if existing["job_id"] == normalized_job_id:
                    return deepcopy(existing)
                raise AutoVodStateValidationError("job_ownership_conflict")
            attempts_value = (
                normalized_attempts
                if normalized_attempts is not None
                else existing["attempts"]
            )
            if attempts_value < existing["attempts"]:
                raise AutoVodStateValidationError("invalid_attempts")
            record = {
                "disposition": "queued",
                "reason": None,
                "attempts": attempts_value,
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
                canonical_streamer, _empty_streamer_bucket()
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
                canonical_streamer, _empty_streamer_bucket()
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
