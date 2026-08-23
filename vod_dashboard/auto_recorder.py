"""Persistent session state for future automatic Twitch recording."""

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


AUTO_RECORDER_STATE_FILE_NAME = "auto-recorder-state.json"
AUTO_RECORDER_STATE_VERSION = 1
AUTO_RECORDER_DISPOSITIONS = frozenset(
    {"pending", "recording", "handled"}
)
MAX_AUTO_RECORD_ATTEMPTS = 1000
MAX_STREAM_ID_LENGTH = 128
MAX_JOB_ID_LENGTH = 128
MAX_REASON_LENGTH = 64

_STREAM_ID_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_STREAM_ID_LENGTH - 1}}}"
)
_JOB_ID_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_JOB_ID_LENGTH - 1}}}"
)
_REASON_RE = re.compile(
    rf"[a-z][a-z0-9_]{{0,{MAX_REASON_LENGTH - 1}}}"
)

State = Dict[str, Any]
Session = Dict[str, Any]
Clock = Callable[[], datetime]
LogCallback = Callable[[str], None]


class AutoRecorderStateError(RuntimeError):
    """Base error for the internal auto-recorder state layer."""


class AutoRecorderStateValidationError(AutoRecorderStateError, ValueError):
    """Raised when a mutation receives unsafe or malformed state metadata."""


class AutoRecorderStateLoadError(AutoRecorderStateError):
    """Raised when an existing persisted state cannot be trusted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AutoRecorderStatePersistenceError(AutoRecorderStateError):
    """Raised when an atomic state write cannot be completed."""


def empty_auto_recorder_state() -> State:
    return {"version": AUTO_RECORDER_STATE_VERSION, "sessions": {}}


def auto_recorder_state_path(dashboard_dir: Path) -> Path:
    """Resolve state below the same persistent dashboard data directory."""
    return Path(dashboard_dir) / AUTO_RECORDER_STATE_FILE_NAME


def _warn(log: Optional[LogCallback], message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _normalize_stream_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _STREAM_ID_RE.fullmatch(candidate) else ""


def _normalize_attempts(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_AUTO_RECORD_ATTEMPTS:
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
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + "+00:00"
            if candidate.endswith("Z")
            else candidate
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


def _normalize_session(value: Any) -> Optional[Session]:
    if not isinstance(value, Mapping):
        return None
    stream_id = _normalize_stream_id(value.get("stream_id"))
    disposition = str(value.get("disposition") or "").strip()
    attempts = _normalize_attempts(value.get("attempts"))
    updated_at = _normalize_utc_timestamp(value.get("updated_at"))
    if (
        not stream_id
        or disposition not in AUTO_RECORDER_DISPOSITIONS
        or attempts is None
        or updated_at is None
    ):
        return None
    return {
        "stream_id": stream_id,
        "disposition": disposition,
        "reason": _normalize_reason(value.get("reason")),
        "attempts": attempts,
        "retry_after": _normalize_utc_timestamp(value.get("retry_after")),
        "job_id": _normalize_job_id(value.get("job_id")),
        "updated_at": updated_at,
    }


def normalize_auto_recorder_state(
    value: Any, log: Optional[LogCallback] = None
) -> State:
    """Return a safe version-1 state without trusting persisted input."""
    if not isinstance(value, Mapping):
        _warn(log, "Auto recorder state ignored: invalid top-level value.")
        raise AutoRecorderStateLoadError("invalid_structure")
    version = value.get("version")
    if version is None:
        _warn(log, "Auto recorder state ignored: missing version.")
        raise AutoRecorderStateLoadError("invalid_structure")
    if (
        isinstance(version, bool)
        or version != AUTO_RECORDER_STATE_VERSION
    ):
        _warn(log, "Auto recorder state ignored: unsupported version.")
        raise AutoRecorderStateLoadError("unsupported_version")
    raw_sessions = value.get("sessions")
    if not isinstance(raw_sessions, Mapping):
        _warn(log, "Auto recorder state ignored: invalid sessions value.")
        raise AutoRecorderStateLoadError("invalid_structure")

    sessions: Dict[str, Session] = {}
    discarded = 0
    for raw_streamer, raw_session in raw_sessions.items():
        streamer = canonical_streamer_login(raw_streamer)
        session = _normalize_session(raw_session)
        if not streamer or session is None:
            discarded += 1
            continue
        existing = sessions.get(streamer)
        if (
            existing is None
            or session["updated_at"] > existing["updated_at"]
        ):
            sessions[streamer] = session
    if discarded:
        _warn(
            log,
            f"Auto recorder state discarded {discarded} malformed session(s).",
        )
    return {"version": AUTO_RECORDER_STATE_VERSION, "sessions": sessions}


def _required_streamer(value: Any) -> str:
    streamer = canonical_streamer_login(value)
    if not streamer:
        raise AutoRecorderStateValidationError("invalid_streamer")
    return streamer


def _required_stream_id(value: Any) -> str:
    stream_id = _normalize_stream_id(value)
    if not stream_id:
        raise AutoRecorderStateValidationError("invalid_stream_id")
    return stream_id


def _required_attempts(value: Any) -> int:
    attempts = _normalize_attempts(value)
    if attempts is None:
        raise AutoRecorderStateValidationError("invalid_attempts")
    return attempts


def _optional_reason(value: Any) -> Optional[str]:
    reason = _normalize_reason(value)
    if value is not None and reason is None:
        raise AutoRecorderStateValidationError("invalid_reason")
    return reason


def _optional_job_id(value: Any) -> Optional[str]:
    job_id = _normalize_job_id(value)
    if value is not None and job_id is None:
        raise AutoRecorderStateValidationError("invalid_job_id")
    return job_id


def _optional_retry_after(value: Any) -> Optional[str]:
    retry_after = _normalize_utc_timestamp(value)
    if value is not None and retry_after is None:
        raise AutoRecorderStateValidationError("invalid_retry_after")
    return retry_after


def _timestamp_from_clock(clock: Clock) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise AutoRecorderStateValidationError("invalid_clock")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def reconcile_interrupted_recordings(
    state: Any, updated_at: str
) -> tuple[State, int]:
    """Purely mark persisted recording sessions as restart-interrupted."""
    normalized = normalize_auto_recorder_state(state)
    timestamp = _normalize_utc_timestamp(updated_at)
    if timestamp is None:
        raise AutoRecorderStateValidationError("invalid_updated_at")
    reconciled = deepcopy(normalized)
    changed = 0
    for session in reconciled["sessions"].values():
        if session["disposition"] != "recording":
            continue
        session["disposition"] = "handled"
        session["reason"] = "restart_interrupted"
        session["retry_after"] = None
        session["updated_at"] = timestamp
        changed += 1
    return reconciled, changed


class AutoRecorderStateStore:
    """Thread-safe, disk-backed state with atomic read-modify-write mutations."""

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
    ) -> "AutoRecorderStateStore":
        return cls(
            auto_recorder_state_path(dashboard_dir), clock=clock, log=log
        )

    def _load_locked(self) -> State:
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return empty_auto_recorder_state()
        except Exception as exc:
            _warn(self._log, "Auto recorder state could not be read.")
            raise AutoRecorderStateLoadError("unreadable_state") from exc
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            _warn(self._log, "Auto recorder state ignored: invalid JSON.")
            raise AutoRecorderStateLoadError("invalid_json") from exc
        return normalize_auto_recorder_state(value, self._log)

    def load(self) -> State:
        with self._lock:
            return self._load_locked()

    def snapshot(self) -> State:
        return self.load()

    def get_session(self, streamer: Any) -> Optional[Session]:
        canonical = canonical_streamer_login(streamer)
        if not canonical:
            return None
        with self._lock:
            session = self._load_locked()["sessions"].get(canonical)
            return deepcopy(session) if session is not None else None

    def session_matches(self, streamer: Any, stream_id: Any) -> bool:
        canonical = canonical_streamer_login(streamer)
        normalized_stream_id = _normalize_stream_id(stream_id)
        if not canonical or not normalized_stream_id:
            return False
        session = self.get_session(canonical)
        return bool(
            session and session.get("stream_id") == normalized_stream_id
        )

    def _write_locked(self, state: State) -> None:
        normalized = normalize_auto_recorder_state(state)
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
        except Exception as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise AutoRecorderStatePersistenceError(
                "Could not persist auto recorder state."
            ) from exc

    def _persist_session_locked(
        self, state: State, streamer: str, session: Session
    ) -> Session:
        state["sessions"][streamer] = session
        self._write_locked(state)
        return deepcopy(session)

    def set_pending(
        self,
        streamer: Any,
        stream_id: Any,
        *,
        attempts: int = 0,
        retry_after: Optional[str] = None,
    ) -> Session:
        canonical = _required_streamer(streamer)
        normalized_stream_id = _required_stream_id(stream_id)
        normalized_attempts = _required_attempts(attempts)
        normalized_retry_after = _optional_retry_after(retry_after)
        with self._lock:
            state = self._load_locked()
            existing = state["sessions"].get(canonical)
            if (
                existing
                and existing["stream_id"] == normalized_stream_id
                and existing["disposition"] in {"recording", "handled"}
            ):
                return deepcopy(existing)
            session = {
                "stream_id": normalized_stream_id,
                "disposition": "pending",
                "reason": None,
                "attempts": normalized_attempts,
                "retry_after": normalized_retry_after,
                "job_id": None,
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_session_locked(state, canonical, session)

    def set_recording(
        self,
        streamer: Any,
        stream_id: Any,
        *,
        job_id: Optional[str] = None,
        attempts: int = 1,
    ) -> Session:
        canonical = _required_streamer(streamer)
        normalized_stream_id = _required_stream_id(stream_id)
        normalized_job_id = _optional_job_id(job_id)
        normalized_attempts = _required_attempts(attempts)
        with self._lock:
            state = self._load_locked()
            existing = state["sessions"].get(canonical)
            if (
                existing
                and existing["stream_id"] == normalized_stream_id
                and existing["disposition"] == "handled"
            ):
                return deepcopy(existing)
            session = {
                "stream_id": normalized_stream_id,
                "disposition": "recording",
                "reason": None,
                "attempts": normalized_attempts,
                "retry_after": None,
                "job_id": normalized_job_id,
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_session_locked(state, canonical, session)

    def set_handled(
        self,
        streamer: Any,
        stream_id: Any,
        reason: Optional[str],
        *,
        attempts: Optional[int] = None,
        job_id: Optional[str] = None,
    ) -> Session:
        canonical = _required_streamer(streamer)
        normalized_stream_id = _required_stream_id(stream_id)
        normalized_reason = _optional_reason(reason)
        normalized_job_id = _optional_job_id(job_id)
        with self._lock:
            state = self._load_locked()
            existing = state["sessions"].get(canonical)
            same_session = bool(
                existing and existing["stream_id"] == normalized_stream_id
            )
            normalized_attempts = (
                _required_attempts(attempts)
                if attempts is not None
                else int(existing["attempts"] if same_session else 0)
            )
            if normalized_job_id is None and same_session:
                normalized_job_id = existing.get("job_id")
            session = {
                "stream_id": normalized_stream_id,
                "disposition": "handled",
                "reason": normalized_reason,
                "attempts": normalized_attempts,
                "retry_after": None,
                "job_id": normalized_job_id,
                "updated_at": _timestamp_from_clock(self._clock),
            }
            return self._persist_session_locked(state, canonical, session)

    def update_retry(
        self,
        streamer: Any,
        stream_id: Any,
        *,
        attempts: int,
        retry_after: Optional[str],
    ) -> Session:
        canonical = _required_streamer(streamer)
        normalized_stream_id = _required_stream_id(stream_id)
        normalized_attempts = _required_attempts(attempts)
        normalized_retry_after = _optional_retry_after(retry_after)
        with self._lock:
            state = self._load_locked()
            existing = state["sessions"].get(canonical)
            if not existing or existing["stream_id"] != normalized_stream_id:
                raise AutoRecorderStateValidationError("session_not_found")
            if existing["disposition"] == "handled":
                return deepcopy(existing)
            session = deepcopy(existing)
            session["attempts"] = normalized_attempts
            session["retry_after"] = normalized_retry_after
            session["updated_at"] = _timestamp_from_clock(self._clock)
            return self._persist_session_locked(state, canonical, session)

    def mark_interrupted_recordings_handled(self) -> int:
        with self._lock:
            state = self._load_locked()
            reconciled, changed = reconcile_interrupted_recordings(
                state, _timestamp_from_clock(self._clock)
            )
            if changed:
                self._write_locked(reconciled)
            return changed
