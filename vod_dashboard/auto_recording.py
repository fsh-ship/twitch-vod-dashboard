"""One deterministic, explicitly invoked automatic-recording iteration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
from typing import Any, Callable, Dict, Mapping, Optional

from vod_dashboard.auto_recorder import (
    MAX_AUTO_RECORD_ATTEMPTS,
    AutoRecorderStateError,
    AutoRecorderStateLoadError,
    AutoRecorderStateStore,
    normalize_auto_recorder_stream_id,
)
from vod_dashboard.settings import (
    canonical_streamer_login,
    normalize_streamer_profiles,
)


SettingsProvider = Callable[[], Mapping[str, Any]]
StreamerProvider = Callable[[Mapping[str, Any]], list[str]]
LiveStatusChecker = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
RecordingStarter = Callable[..., str]
ExecutorFactory = Callable[..., Any]

_SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _safe_reason(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_REASON_RE.fullmatch(candidate) else fallback


def _safe_live_metadata(
    streamer: str, stream_id: str, value: Mapping[str, Any]
) -> Dict[str, Any]:
    started_at = value.get("started_at")
    return {
        "state": "live",
        "streamer": streamer,
        "stream_id": stream_id,
        "title": str(value.get("title") or "")[:500],
        "started_at": (
            str(started_at)[:64] if started_at is not None else None
        ),
    }


class AutoRecorderCoordinator:
    """Run one bounded Auto Recorder decision cycle with injected I/O."""

    def __init__(
        self,
        *,
        settings_provider: SettingsProvider,
        streamer_provider: StreamerProvider,
        live_status_checker: LiveStatusChecker,
        state_store: AutoRecorderStateStore,
        recording_starter: RecordingStarter,
        executor_factory: ExecutorFactory = ThreadPoolExecutor,
        max_status_workers: int = 2,
    ) -> None:
        self._settings_provider = settings_provider
        self._streamer_provider = streamer_provider
        self._live_status_checker = live_status_checker
        self._state_store = state_store
        self._recording_starter = recording_starter
        self._executor_factory = executor_factory
        self._max_status_workers = max(1, min(2, int(max_status_workers)))

    @staticmethod
    def _result(
        *, enabled: bool, watched_count: int, state_healthy: Optional[bool]
    ) -> Dict[str, Any]:
        return {
            "enabled": enabled,
            "state_healthy": state_healthy,
            "watched_count": watched_count,
            "checked_count": 0,
            "live_count": 0,
            "offline_count": 0,
            "error_count": 0,
            "action": "idle",
            "outcomes": [],
        }

    @staticmethod
    def _watched_streamers(
        settings: Mapping[str, Any], configured: list[str]
    ) -> list[str]:
        profiles = normalize_streamer_profiles(
            settings.get("streamer_profiles")
        )
        watched = []
        seen = set()
        for raw_streamer in configured:
            streamer = canonical_streamer_login(raw_streamer)
            if not streamer or streamer in seen:
                continue
            seen.add(streamer)
            if profiles.get(streamer, {}).get("auto_record") is True:
                watched.append(streamer)
        return watched

    def _check_status(
        self, streamer: str, settings: Mapping[str, Any]
    ) -> Dict[str, Any]:
        try:
            raw = self._live_status_checker(streamer, settings)
        except Exception:
            return {
                "streamer": streamer,
                "status": "error",
                "reason": "status_check_failed",
            }
        if not isinstance(raw, Mapping):
            return {
                "streamer": streamer,
                "status": "error",
                "reason": "invalid_status_result",
            }
        state = str(raw.get("state") or "").strip().lower()
        if state == "offline":
            return {"streamer": streamer, "status": "offline"}
        if state != "live":
            return {
                "streamer": streamer,
                "status": "error",
                "reason": "unknown_live_state",
            }
        stream_id = normalize_auto_recorder_stream_id(raw.get("stream_id"))
        if not stream_id:
            return {
                "streamer": streamer,
                "status": "live",
                "decision": "missing_stream_id",
            }
        return {
            "streamer": streamer,
            "status": "live",
            "stream_id": stream_id,
            "live_metadata": _safe_live_metadata(streamer, stream_id, raw),
        }

    @staticmethod
    def _state_error_result(
        result: Dict[str, Any], error: AutoRecorderStateError
    ) -> Dict[str, Any]:
        result["state_healthy"] = False
        result["action"] = "state_unhealthy"
        result["reason"] = _safe_reason(
            getattr(error, "reason", ""), "state_persistence_failed"
        )
        return result

    def run_once(self) -> Dict[str, Any]:
        settings = dict(self._settings_provider() or {})
        if settings.get("auto_recorder_enabled") is not True:
            result = self._result(
                enabled=False, watched_count=0, state_healthy=None
            )
            result["action"] = "disabled"
            return result

        configured = list(self._streamer_provider(settings) or [])
        watched = self._watched_streamers(settings, configured)
        result = self._result(
            enabled=True,
            watched_count=len(watched),
            state_healthy=None,
        )
        if not watched:
            return result

        try:
            persisted = self._state_store.load()
        except AutoRecorderStateLoadError as exc:
            return self._state_error_result(result, exc)
        result["state_healthy"] = True

        observations: Dict[str, Dict[str, Any]] = {}
        workers = min(self._max_status_workers, len(watched))
        with self._executor_factory(max_workers=workers) as executor:
            futures = {
                streamer: executor.submit(
                    self._check_status, streamer, settings
                )
                for streamer in watched
            }
            for streamer in watched:
                observations[streamer] = futures[streamer].result()

        result["checked_count"] = len(watched)
        result["live_count"] = sum(
            item["status"] == "live" for item in observations.values()
        )
        result["offline_count"] = sum(
            item["status"] == "offline" for item in observations.values()
        )
        result["error_count"] = sum(
            item["status"] == "error" for item in observations.values()
        )

        candidates = []
        sessions = dict(persisted.get("sessions") or {})
        for streamer in watched:
            observation = observations[streamer]
            outcome = {
                key: value
                for key, value in observation.items()
                if key != "live_metadata"
            }
            if observation["status"] != "live" or not observation.get(
                "stream_id"
            ):
                result["outcomes"].append(outcome)
                continue

            stream_id = observation["stream_id"]
            existing = sessions.get(streamer)
            if existing and existing.get("stream_id") == stream_id:
                disposition = existing.get("disposition")
                if disposition == "handled":
                    outcome["decision"] = "already_handled"
                elif disposition == "recording":
                    outcome["decision"] = "already_recording"
                else:
                    outcome["decision"] = "pending"
                    candidates.append(
                        (streamer, stream_id, existing, observation)
                    )
                result["outcomes"].append(outcome)
                continue

            try:
                pending = self._state_store.set_pending(
                    streamer, stream_id, attempts=0
                )
            except AutoRecorderStateError as exc:
                result["outcomes"].append(outcome)
                return self._state_error_result(result, exc)
            sessions[streamer] = pending
            outcome["decision"] = "pending"
            result["outcomes"].append(outcome)
            candidates.append((streamer, stream_id, pending, observation))

        if not candidates:
            return result

        streamer, stream_id, session, observation = candidates[0]
        previous_attempts = int(session.get("attempts") or 0)
        next_attempt = previous_attempts + 1
        if next_attempt > MAX_AUTO_RECORD_ATTEMPTS:
            result["action"] = "start_failed"
            result["reason"] = "attempt_limit_reached"
            result.update({"streamer": streamer, "stream_id": stream_id})
            return result

        try:
            reservation = self._state_store.set_recording(
                streamer,
                stream_id,
                attempts=next_attempt,
            )
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        if reservation.get("disposition") != "recording":
            result["action"] = "idle"
            return result

        try:
            job_id = str(
                self._recording_starter(
                    streamer,
                    live_metadata=observation["live_metadata"],
                    origin="auto",
                    attempt=next_attempt,
                )
                or ""
            ).strip()
            if not job_id:
                raise RuntimeError("recording starter returned no job id")
        except Exception as exc:
            reason = _safe_reason(
                getattr(exc, "reason", ""), "recording_start_failed"
            )
            consumed_attempts = (
                previous_attempts
                if reason == "recording_conflict"
                else next_attempt
            )
            try:
                self._state_store.return_to_pending(
                    streamer,
                    stream_id,
                    attempts=consumed_attempts,
                )
            except AutoRecorderStateError as state_exc:
                return self._state_error_result(result, state_exc)
            result["action"] = reason if reason == "recording_conflict" else "start_failed"
            result["reason"] = reason
            result.update(
                {
                    "streamer": streamer,
                    "stream_id": stream_id,
                    "attempt": consumed_attempts,
                }
            )
            return result

        try:
            self._state_store.set_recording(
                streamer,
                stream_id,
                job_id=job_id,
                attempts=next_attempt,
            )
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        result["action"] = "recording_started"
        result.update(
            {
                "streamer": streamer,
                "stream_id": stream_id,
                "job_id": job_id,
                "attempt": next_attempt,
            }
        )
        return result
