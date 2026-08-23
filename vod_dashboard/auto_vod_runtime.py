"""Process-local lifecycle wrapper for the deterministic Auto VOD coordinator."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Callable, Dict, Mapping, Optional


DEFAULT_AUTO_VOD_STARTUP_DELAY_SECONDS = 7.0
DEFAULT_AUTO_VOD_JOIN_TIMEOUT_SECONDS = 5.0


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class AutoVodMonitor:
    """Run one coordinator sequentially with interruptible production timing."""

    def __init__(
        self,
        coordinator: Any,
        *,
        settings_provider: Callable[[], Mapping[str, Any]],
        startup_delay_seconds: float = DEFAULT_AUTO_VOD_STARTUP_DELAY_SECONDS,
        join_timeout_seconds: float = DEFAULT_AUTO_VOD_JOIN_TIMEOUT_SECONDS,
        stop_event: Optional[threading.Event] = None,
        clock: Optional[Callable[[], datetime]] = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._coordinator = coordinator
        self._settings_provider = settings_provider
        self._startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self._join_timeout_seconds = max(0.0, float(join_timeout_seconds))
        self._stop_event = stop_event or threading.Event()
        self._wake_event = threading.Event()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_factory = thread_factory
        self._log = log
        self._lifecycle_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {
            "running": False, "thread_alive": False, "in_progress": False,
            "last_started_at": None, "last_finished_at": None,
            "last_result": None, "next_check_at": None, "wake_pending": False,
        }

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def snapshot(self) -> Dict[str, Any]:
        with self._status_lock:
            value = deepcopy(self._status)
        value["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return value

    def _update(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _interval_seconds(self) -> float:
        try:
            minutes = int(self._settings_provider().get("auto_vod_poll_minutes", 60))
        except Exception:
            minutes = 60
        return float(minutes * 60 if minutes in {60, 120} else 3600)

    def _wait(self, seconds: float) -> bool:
        self._wake_event.wait(max(0.0, seconds))
        self._wake_event.clear()
        return self._stop_event.is_set()

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            now = _utc_now(self._clock)
            self._update(running=True, thread_alive=True, in_progress=False,
                         next_check_at=_timestamp(now + timedelta(seconds=self._startup_delay_seconds)),
                         wake_pending=False)
            thread = self._thread_factory(target=self._run, name="auto-vod-monitor", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._update(running=False, thread_alive=False, next_check_at=None)
                raise
            return True

    def wake(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is None or not self._thread.is_alive() or self._stop_event.is_set():
                return False
            self._update(wake_pending=True, next_check_at=None)
            self._wake_event.set()
            return True

    def stop(self, timeout: Optional[float] = None) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
            self._wake_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._join_timeout_seconds if timeout is None else max(0.0, float(timeout)))
        alive = bool(thread and thread.is_alive())
        if not alive:
            self._update(running=False, thread_alive=False, in_progress=False, next_check_at=None, wake_pending=False)
        return not alive

    def _safe_error_result(self) -> Dict[str, Any]:
        return {"enabled": False, "state_healthy": None, "action": "monitor_error", "error_count": 1, "errors": [{"code": "monitor_error"}]}

    def _run(self) -> None:
        try:
            if self._wait(self._startup_delay_seconds):
                return
            while not self._stop_event.is_set():
                started = _utc_now(self._clock)
                self._update(in_progress=True, wake_pending=False, last_started_at=_timestamp(started), next_check_at=None)
                try:
                    raw = self._coordinator.run_once()
                    result = dict(raw) if isinstance(raw, Mapping) else self._safe_error_result()
                except Exception:
                    result = self._safe_error_result()
                    if self._log is not None:
                        try:
                            self._log("Auto VOD monitor iteration failed (monitor_error).")
                        except Exception:
                            pass
                finished = _utc_now(self._clock)
                interval = self._interval_seconds()
                self._update(in_progress=False, last_finished_at=_timestamp(finished), last_result=result,
                             next_check_at=_timestamp(finished + timedelta(seconds=interval)))
                if self._wait(interval):
                    return
        finally:
            self._update(running=False, thread_alive=False, in_progress=False, next_check_at=None, wake_pending=False)
