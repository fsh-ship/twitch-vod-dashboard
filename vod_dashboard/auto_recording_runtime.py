"""Process-local runtime lifecycle for the deterministic Auto Recorder."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
import threading
from typing import Any, Callable, Dict, Mapping, Optional


DEFAULT_AUTO_RECORDER_INTERVAL_SECONDS = 60.0
DEFAULT_AUTO_RECORDER_STARTUP_DELAY_SECONDS = 5.0
DEFAULT_AUTO_RECORDER_JOIN_TIMEOUT_SECONDS = 5.0

Clock = Callable[[], datetime]
LogCallback = Callable[[str], None]
ThreadFactory = Callable[..., threading.Thread]

_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SAFE_ACTIONS = frozenset(
    {
        "cooldown",
        "disabled",
        "idle",
        "manual_stop",
        "missing_job",
        "recording_completed",
        "recording_conflict",
        "recording_started",
        "reconciliation_error",
        "retry_exhausted",
        "retry_scheduled",
        "shutdown_requested",
        "start_failed",
        "state_persistence_failed",
        "state_unhealthy",
    }
)


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise ValueError("invalid_clock")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _safe_code(value: Any, fallback: str = "none") -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_CODE_RE.fullmatch(candidate) else fallback


def _safe_action(value: Any) -> str:
    candidate = _safe_code(value)
    return candidate if candidate in _SAFE_ACTIONS else "none"


def _safe_streamer(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if re.fullmatch(r"[a-z0-9_]{1,25}", candidate) else ""


class AutoRecorderMonitor:
    """Run one coordinator sequentially with interruptible lifecycle timing."""

    def __init__(
        self,
        coordinator: Any,
        *,
        interval_seconds: float = DEFAULT_AUTO_RECORDER_INTERVAL_SECONDS,
        startup_delay_seconds: float = DEFAULT_AUTO_RECORDER_STARTUP_DELAY_SECONDS,
        join_timeout_seconds: float = DEFAULT_AUTO_RECORDER_JOIN_TIMEOUT_SECONDS,
        stop_event: Optional[threading.Event] = None,
        clock: Optional[Clock] = None,
        log: Optional[LogCallback] = None,
        thread_factory: ThreadFactory = threading.Thread,
    ) -> None:
        self._coordinator = coordinator
        self._interval_seconds = max(0.0, float(interval_seconds))
        self._startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self._join_timeout_seconds = max(0.0, float(join_timeout_seconds))
        self._stop_event = stop_event or threading.Event()
        self._wake_event = threading.Event()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._log = log
        self._thread_factory = thread_factory
        self._lifecycle_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {
            "running": False,
            "enabled": False,
            "state_healthy": None,
            "watched_count": 0,
            "phase": "stopped",
            "last_check_started_at": None,
            "last_check_completed_at": None,
            "next_check_at": None,
            "last_action": "none",
            "last_action_streamer": "",
            "error_count_last_run": 0,
            "last_error_code": "",
        }

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def snapshot(self) -> Dict[str, Any]:
        with self._status_lock:
            return deepcopy(self._status)

    def _update_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _safe_log(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log(message)
        except Exception:
            pass

    def _wait(self, timeout: float) -> bool:
        self._wake_event.wait(timeout=max(0.0, timeout))
        self._wake_event.clear()
        return self._stop_event.is_set()

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            now = _utc_now(self._clock)
            self._update_status(
                running=True,
                phase="starting",
                next_check_at=_utc_timestamp(
                    now + timedelta(seconds=self._startup_delay_seconds)
                ),
                last_error_code="",
            )
            thread = self._thread_factory(
                target=self._run,
                name="auto-recorder-monitor",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._update_status(
                    running=False,
                    phase="stopped",
                    next_check_at=None,
                    last_error_code="thread_start_failed",
                )
                raise
            return True

    def wake(self) -> bool:
        with self._lifecycle_lock:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._stop_event.is_set()
            ):
                return False
            self._wake_event.set()
            return True

    def stop(self, timeout: Optional[float] = None) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
            self._wake_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(
                timeout=(
                    self._join_timeout_seconds
                    if timeout is None
                    else max(0.0, float(timeout))
                )
            )
        alive = bool(thread is not None and thread.is_alive())
        if not alive:
            self._update_status(
                running=False, phase="stopped", next_check_at=None
            )
        return not alive

    def _record_unexpected_error(self, exc: BaseException) -> None:
        type_name = type(exc).__name__
        safe_type = (
            type_name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", type_name) else "Error"
        )
        self._safe_log(
            f"Auto recorder monitor iteration failed ({safe_type})."
        )
        self._update_status(
            phase="degraded",
            state_healthy=False,
            error_count_last_run=1,
            last_error_code="unexpected_iteration_error",
            last_action="none",
            last_action_streamer="",
        )

    def _apply_result(self, result: Mapping[str, Any]) -> str:
        enabled = result.get("enabled") is True
        healthy = result.get("state_healthy")
        if healthy is not True and healthy is not False and healthy is not None:
            healthy = None
        try:
            watched = max(0, min(10000, int(result.get("watched_count") or 0)))
        except (TypeError, ValueError, OverflowError):
            watched = 0
        try:
            error_count = max(0, min(10000, int(result.get("error_count") or 0)))
        except (TypeError, ValueError, OverflowError):
            error_count = 0
        action = _safe_action(result.get("action"))
        error_code = ""
        if healthy is False or error_count:
            error_code = _safe_code(result.get("reason"), "status_check_failed")
        phase = "degraded" if healthy is False else "paused" if not enabled else "sleeping"
        self._update_status(
            enabled=enabled,
            state_healthy=healthy,
            watched_count=watched,
            phase=phase,
            last_action=action,
            last_action_streamer=_safe_streamer(result.get("streamer")),
            error_count_last_run=error_count,
            last_error_code=error_code,
        )
        return phase

    def _run(self) -> None:
        try:
            try:
                prepared = self._coordinator.prepare_after_restart()
                if isinstance(prepared, Mapping):
                    if prepared.get("state_healthy") is False:
                        self._update_status(
                            phase="degraded",
                            state_healthy=False,
                            last_action=_safe_action(prepared.get("action")),
                            last_error_code=_safe_code(
                                prepared.get("reason"), "state_unhealthy"
                            ),
                        )
            except Exception as exc:
                self._record_unexpected_error(exc)

            if self._wait(self._startup_delay_seconds):
                return

            while not self._stop_event.is_set():
                started = _utc_now(self._clock)
                self._update_status(
                    phase="checking",
                    last_check_started_at=_utc_timestamp(started),
                    next_check_at=None,
                )
                try:
                    raw_result = self._coordinator.run_once()
                    result = raw_result if isinstance(raw_result, Mapping) else {}
                    self._apply_result(result)
                except Exception as exc:
                    self._record_unexpected_error(exc)
                completed = _utc_now(self._clock)
                self._update_status(
                    last_check_completed_at=_utc_timestamp(completed),
                    next_check_at=_utc_timestamp(
                        completed + timedelta(seconds=self._interval_seconds)
                    ),
                )
                if self._wait(self._interval_seconds):
                    return
        finally:
            self._update_status(
                running=False, phase="stopped", next_check_at=None
            )
