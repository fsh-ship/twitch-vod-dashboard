"""Deterministic, explicitly invoked automatic-recording lifecycle logic."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from vod_dashboard.auto_recorder import (
    AutoRecorderStateError,
    AutoRecorderStatePersistenceError,
    AutoRecorderStateStore,
    normalize_auto_recorder_stream_id,
)
from vod_dashboard.settings import canonical_streamer_login, normalize_streamer_profiles


SettingsProvider = Callable[[], Mapping[str, Any]]
StreamerProvider = Callable[[Mapping[str, Any]], list[str]]
LiveStatusChecker = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
RecordingStarter = Callable[..., str]
RecordingJobsProvider = Callable[[], Iterable[Mapping[str, Any]]]
ExecutorFactory = Callable[..., Any]
Clock = Callable[[], datetime]

START_FAILURE_COOLDOWNS = {1: 60, 2: 120}
MAX_START_ATTEMPTS = 3
PROCESS_FAILURE_COOLDOWN_SECONDS = 120
MAX_PROCESS_ATTEMPTS = 2
_SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SAFE_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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
        "started_at": str(started_at)[:64] if started_at is not None else None,
    }


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


def _parse_utc_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class AutoRecorderCoordinator:
    """Run bounded Auto Recorder decisions with injected, offline-testable I/O."""

    def __init__(
        self,
        *,
        settings_provider: SettingsProvider,
        streamer_provider: StreamerProvider,
        live_status_checker: LiveStatusChecker,
        state_store: AutoRecorderStateStore,
        recording_starter: RecordingStarter,
        recording_jobs_provider: Optional[RecordingJobsProvider] = None,
        clock: Optional[Clock] = None,
        executor_factory: ExecutorFactory = ThreadPoolExecutor,
        max_status_workers: int = 2,
    ) -> None:
        self._settings_provider = settings_provider
        self._streamer_provider = streamer_provider
        self._live_status_checker = live_status_checker
        self._state_store = state_store
        self._recording_starter = recording_starter
        self._recording_jobs_provider = recording_jobs_provider or (lambda: ())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
        profiles = normalize_streamer_profiles(settings.get("streamer_profiles"))
        watched, seen = [], set()
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
            return {"streamer": streamer, "status": "error", "reason": "status_check_failed"}
        if not isinstance(raw, Mapping):
            return {"streamer": streamer, "status": "error", "reason": "invalid_status_result"}
        state = str(raw.get("state") or "").strip().lower()
        if state == "offline":
            return {"streamer": streamer, "status": "offline"}
        if state != "live":
            return {"streamer": streamer, "status": "error", "reason": "unknown_live_state"}
        stream_id = normalize_auto_recorder_stream_id(raw.get("stream_id"))
        if not stream_id:
            return {"streamer": streamer, "status": "live", "decision": "missing_stream_id"}
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
        if isinstance(error, AutoRecorderStatePersistenceError):
            result["action"] = "state_persistence_failed"
            result["reason"] = "state_persistence_failed"
        else:
            result["action"] = "state_unhealthy"
            result["reason"] = _safe_reason(
                getattr(error, "reason", ""), "state_persistence_failed"
            )
        return result

    def _retry_after(self, seconds: int) -> str:
        return _utc_timestamp(_utc_now(self._clock) + timedelta(seconds=seconds))

    @staticmethod
    def _safe_job(job: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        if str(job.get("type") or "") != "recording":
            return None
        raw_job_id = str(job.get("id") or "").strip()
        return {
            "id": raw_job_id if _SAFE_JOB_ID_RE.fullmatch(raw_job_id) else "",
            "state": str(job.get("state") or "").strip(),
            "origin": str(job.get("origin") or "").strip(),
            "attempt": job.get("attempt"),
            "streamer": canonical_streamer_login(job.get("streamer")),
            "stream_id": normalize_auto_recorder_stream_id(job.get("stream_id")),
            "completion_reason": _safe_reason(job.get("completion_reason"), "unknown"),
            "output_complete": job.get("output_complete") is True,
            "stop_requested": job.get("stop_requested") is True,
        }

    @staticmethod
    def _match_job(
        streamer: str,
        session: Mapping[str, Any],
        jobs: list[Dict[str, Any]],
    ) -> tuple[Optional[Dict[str, Any]], str]:
        job_id = str(session.get("job_id") or "")
        if job_id:
            match = next((job for job in jobs if job["id"] == job_id), None)
            return match, "job_id" if match is not None else "missing_job"
        matches = [
            job
            for job in jobs
            if job["origin"] == "auto"
            and job["streamer"] == streamer
            and job["stream_id"] == session.get("stream_id")
        ]
        if len(matches) == 1:
            return matches[0], "fallback"
        return None, "ambiguous_job" if len(matches) > 1 else "missing_job"

    def _reconcile_process_failure(
        self,
        streamer: str,
        session: Mapping[str, Any],
        job: Mapping[str, Any],
    ) -> str:
        attempts = int(session.get("attempts") or 0)
        job_attempt = job.get("attempt")
        if isinstance(job_attempt, int) and not isinstance(job_attempt, bool):
            attempts = max(attempts, job_attempt)
        if attempts < MAX_PROCESS_ATTEMPTS:
            self._state_store.return_to_pending(
                streamer,
                session["stream_id"],
                attempts=attempts,
                retry_after=self._retry_after(PROCESS_FAILURE_COOLDOWN_SECONDS),
            )
            return "retry_scheduled"
        self._state_store.set_handled(
            streamer,
            session["stream_id"],
            "retry_exhausted",
            attempts=attempts,
            job_id=job.get("id") or None,
        )
        return "retry_exhausted"

    def reconcile_recording_jobs(
        self,
        jobs: Optional[Iterable[Mapping[str, Any]]] = None,
        *,
        suppressible_streamers: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """Reconcile persistent recording ownership without Twitch I/O or starts."""
        result: Dict[str, Any] = {"state_healthy": None, "action": "idle", "outcomes": []}
        try:
            persisted = self._state_store.load()
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        result["state_healthy"] = True
        try:
            raw_jobs = list(self._recording_jobs_provider() if jobs is None else jobs)
        except Exception:
            result["action"] = "reconciliation_error"
            result["reason"] = "job_snapshot_failed"
            return result
        safe_jobs = [
            safe
            for raw in raw_jobs
            if isinstance(raw, Mapping) and (safe := self._safe_job(raw)) is not None
        ]
        suppressible = {
            canonical_streamer_login(streamer)
            for streamer in suppressible_streamers
            if canonical_streamer_login(streamer)
        }
        stop_intent_jobs = [
            job
            for job in safe_jobs
            if job["stop_requested"]
            and job["streamer"]
            and job["stream_id"]
            and (
                job["origin"] == "auto"
                or job["streamer"] in suppressible
            )
        ]

        for streamer, session in persisted["sessions"].items():
            disposition = session.get("disposition")
            if disposition not in {"recording", "pending"}:
                continue
            outcome = {"streamer": streamer, "stream_id": session["stream_id"]}
            if disposition == "pending":
                stopped_matches = [
                    job
                    for job in stop_intent_jobs
                    if job["streamer"] == streamer
                    and job["stream_id"] == session["stream_id"]
                ]
                if len(stopped_matches) == 1:
                    job, match = stopped_matches[0], "stop_intent"
                elif len(stopped_matches) > 1:
                    outcome["decision"] = "ambiguous_job"
                    result["outcomes"].append(outcome)
                    continue
                else:
                    manual_matches = [
                        job
                        for job in safe_jobs
                        if job["origin"] == "manual"
                        and job["streamer"] == streamer
                        and job["stream_id"] == session["stream_id"]
                    ]
                    if len(manual_matches) != 1:
                        continue
                    job, match = manual_matches[0], "manual_fallback"
            else:
                stopped_matches = [
                    job
                    for job in stop_intent_jobs
                    if job["streamer"] == streamer
                    and job["stream_id"] == session["stream_id"]
                ]
                if len(stopped_matches) == 1:
                    job, match = stopped_matches[0], "stop_intent"
                elif len(stopped_matches) > 1:
                    outcome["decision"] = "ambiguous_job"
                    result["outcomes"].append(outcome)
                    continue
                else:
                    job, match = self._match_job(
                        streamer, session, safe_jobs
                    )
            if job is None:
                outcome["decision"] = match
                result["outcomes"].append(outcome)
                continue
            outcome["job_id"] = job["id"]
            if job["stop_requested"]:
                try:
                    self._state_store.set_handled(
                        streamer,
                        session["stream_id"],
                        "manual_stop",
                        job_id=job["id"] or None,
                    )
                except AutoRecorderStateError as exc:
                    result["outcomes"].append(outcome)
                    return self._state_error_result(result, exc)
                outcome["decision"] = "manual_stop"
                result["outcomes"].append(outcome)
                result["action"] = "manual_stop"
                continue
            if job["state"] not in {"completed", "failed"}:
                outcome["decision"] = "recording"
                result["outcomes"].append(outcome)
                continue

            manual_stop = job["stop_requested"] or job["completion_reason"] in {
                "stopped_by_user", "stop_incomplete", "stop_failed"
            }
            try:
                if manual_stop:
                    self._state_store.set_handled(
                        streamer, session["stream_id"], "manual_stop", job_id=job["id"] or None
                    )
                    decision = "manual_stop"
                elif job["completion_reason"] == "natural_end" and job["output_complete"]:
                    self._state_store.set_handled(
                        streamer, session["stream_id"], "natural_end", job_id=job["id"] or None
                    )
                    decision = "recording_completed"
                elif job["origin"] == "manual":
                    self._state_store.set_handled(
                        streamer,
                        session["stream_id"],
                        "manual_recording_ended",
                        job_id=job["id"] or None,
                    )
                    decision = "recording_completed"
                else:
                    decision = self._reconcile_process_failure(streamer, session, job)
            except AutoRecorderStateError as exc:
                result["outcomes"].append(outcome)
                return self._state_error_result(result, exc)
            outcome["decision"] = decision
            result["outcomes"].append(outcome)
            result["action"] = decision

        try:
            refreshed = self._state_store.load()
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        stop_intents_by_streamer: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for job in stop_intent_jobs:
            stop_intents_by_streamer.setdefault(job["streamer"], {})[
                job["stream_id"]
            ] = job
        for streamer, stream_jobs in stop_intents_by_streamer.items():
            existing = refreshed["sessions"].get(streamer)
            if existing is not None:
                continue
            if len(stream_jobs) != 1:
                result["outcomes"].append(
                    {
                        "streamer": streamer,
                        "decision": "ambiguous_stop_intent",
                    }
                )
                result["action"] = "manual_stop_unresolved"
                continue
            stream_id, job = next(iter(stream_jobs.items()))
            attempt = job.get("attempt")
            if (
                isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 0
            ):
                attempt = 0
            try:
                self._state_store.set_handled(
                    streamer,
                    stream_id,
                    "manual_stop",
                    attempts=attempt,
                    job_id=job["id"] or None,
                )
            except AutoRecorderStateError as exc:
                return self._state_error_result(result, exc)
            result["outcomes"].append(
                {
                    "streamer": streamer,
                    "stream_id": stream_id,
                    "job_id": job["id"],
                    "decision": "manual_stop",
                }
            )
            result["action"] = "manual_stop"
        return result

    def prepare_after_restart(self) -> Dict[str, Any]:
        """Explicitly suppress process-local recordings lost across restart."""
        result: Dict[str, Any] = {
            "state_healthy": None,
            "action": "restart_reconciled",
            "changed_count": 0,
            "outcomes": [],
        }
        try:
            result["changed_count"] = self._state_store.mark_interrupted_recordings_handled()
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        result["state_healthy"] = True
        return result

    def run_once(self) -> Dict[str, Any]:
        settings = dict(self._settings_provider() or {})
        if settings.get("auto_recorder_enabled") is not True:
            result = self._result(enabled=False, watched_count=0, state_healthy=None)
            result["action"] = "disabled"
            return result

        configured = list(self._streamer_provider(settings) or [])
        watched = self._watched_streamers(settings, configured)
        result = self._result(enabled=True, watched_count=len(watched), state_healthy=None)
        if not watched:
            return result

        reconciliation = self.reconcile_recording_jobs(
            suppressible_streamers=watched
        )
        if reconciliation.get("state_healthy") is False:
            result.update(
                {
                    "state_healthy": False,
                    "action": reconciliation["action"],
                    "reason": reconciliation.get("reason", "state_persistence_failed"),
                    "outcomes": reconciliation.get("outcomes", []),
                }
            )
            return result
        if reconciliation.get("action") in {
            "reconciliation_error",
            "manual_stop_unresolved",
        }:
            reconciliation_action = reconciliation["action"]
            result.update(
                {
                    "state_healthy": True,
                    "action": reconciliation_action,
                    "reason": reconciliation.get(
                        "reason",
                        "job_snapshot_failed"
                        if reconciliation_action == "reconciliation_error"
                        else "ambiguous_stop_intent",
                    ),
                }
            )
            return result
        result["state_healthy"] = True
        if reconciliation.get("action") != "idle":
            result["action"] = reconciliation["action"]
        if reconciliation.get("outcomes"):
            result["reconciliation_outcomes"] = reconciliation["outcomes"]
        try:
            persisted = self._state_store.load()
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)

        observations: Dict[str, Dict[str, Any]] = {}
        workers = min(self._max_status_workers, len(watched))
        with self._executor_factory(max_workers=workers) as executor:
            futures = {
                streamer: executor.submit(self._check_status, streamer, settings)
                for streamer in watched
            }
            for streamer in watched:
                observations[streamer] = futures[streamer].result()

        result["checked_count"] = len(watched)
        result["live_count"] = sum(item["status"] == "live" for item in observations.values())
        result["offline_count"] = sum(item["status"] == "offline" for item in observations.values())
        result["error_count"] = sum(item["status"] == "error" for item in observations.values())

        candidates = []
        sessions = dict(persisted.get("sessions") or {})
        now = _utc_now(self._clock)
        for streamer in watched:
            observation = observations[streamer]
            outcome = {key: value for key, value in observation.items() if key != "live_metadata"}
            if observation["status"] != "live" or not observation.get("stream_id"):
                result["outcomes"].append(outcome)
                continue

            stream_id = observation["stream_id"]
            existing = sessions.get(streamer)
            if (
                existing
                and existing.get("disposition") == "recording"
                and existing.get("stream_id") != stream_id
            ):
                outcome["decision"] = "recording_state_unresolved"
                result["outcomes"].append(outcome)
                continue
            if existing and existing.get("stream_id") == stream_id:
                disposition = existing.get("disposition")
                if disposition == "handled":
                    outcome["decision"] = "already_handled"
                elif disposition == "recording":
                    outcome["decision"] = "already_recording"
                else:
                    retry_after = _parse_utc_timestamp(existing.get("retry_after"))
                    if retry_after is not None and retry_after > now:
                        outcome["decision"] = "cooldown"
                        outcome["retry_after"] = _utc_timestamp(retry_after)
                    elif int(existing.get("attempts") or 0) >= MAX_START_ATTEMPTS:
                        try:
                            self._state_store.set_handled(
                                streamer,
                                stream_id,
                                "retry_exhausted",
                                attempts=int(existing.get("attempts") or 0),
                            )
                        except AutoRecorderStateError as exc:
                            result["outcomes"].append(outcome)
                            return self._state_error_result(result, exc)
                        outcome["decision"] = "retry_exhausted"
                    else:
                        outcome["decision"] = "pending"
                        candidates.append((streamer, stream_id, existing, observation))
                result["outcomes"].append(outcome)
                continue

            try:
                pending = self._state_store.set_pending(streamer, stream_id, attempts=0)
            except AutoRecorderStateError as exc:
                result["outcomes"].append(outcome)
                return self._state_error_result(result, exc)
            sessions[streamer] = pending
            outcome["decision"] = "pending"
            result["outcomes"].append(outcome)
            candidates.append((streamer, stream_id, pending, observation))

        if not candidates:
            return result

        unresolved = next(
            (
                outcome["decision"]
                for outcome in reconciliation.get("outcomes", [])
                if outcome.get("decision")
                in {"missing_job", "ambiguous_job"}
            ),
            None,
        )
        if unresolved is not None:
            result["action"] = unresolved
            return result

        streamer, stream_id, session, observation = candidates[0]
        previous_attempts = int(session.get("attempts") or 0)
        next_attempt = previous_attempts + 1
        try:
            reservation = self._state_store.set_recording(
                streamer, stream_id, attempts=next_attempt
            )
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        if reservation.get("disposition") != "recording":
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
            reason = _safe_reason(getattr(exc, "reason", ""), "recording_start_failed")
            consumed_attempts = previous_attempts if reason == "recording_conflict" else next_attempt
            try:
                if reason == "recording_conflict":
                    self._state_store.return_to_pending(
                        streamer, stream_id, attempts=previous_attempts
                    )
                elif consumed_attempts >= MAX_START_ATTEMPTS:
                    self._state_store.set_handled(
                        streamer, stream_id, "retry_exhausted", attempts=consumed_attempts
                    )
                else:
                    self._state_store.return_to_pending(
                        streamer,
                        stream_id,
                        attempts=consumed_attempts,
                        retry_after=self._retry_after(START_FAILURE_COOLDOWNS[consumed_attempts]),
                    )
            except AutoRecorderStateError as state_exc:
                return self._state_error_result(result, state_exc)
            exhausted = consumed_attempts >= MAX_START_ATTEMPTS
            result["action"] = reason if reason == "recording_conflict" else "retry_exhausted" if exhausted else "start_failed"
            result["reason"] = "retry_exhausted" if exhausted else reason
            result.update(
                {"streamer": streamer, "stream_id": stream_id, "attempt": consumed_attempts}
            )
            return result

        try:
            self._state_store.set_recording(
                streamer, stream_id, job_id=job_id, attempts=next_attempt
            )
        except AutoRecorderStateError as exc:
            return self._state_error_result(result, exc)
        result["action"] = "recording_started"
        result.update(
            {"streamer": streamer, "stream_id": stream_id, "job_id": job_id, "attempt": next_attempt}
        )
        return result
