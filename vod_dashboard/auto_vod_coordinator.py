"""One deterministic, dependency-injected Auto VOD decision cycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from vod_dashboard.auto_vod import AutoVodStateError, AutoVodStateStore
from vod_dashboard.settings import canonical_streamer_login, normalize_streamer_profiles
from vod_dashboard.twitch import canonical_twitch_vod_url, discover_streamer_vods


MAX_DISCOVERY_WORKERS = 2
MAX_NEW_JOBS_PER_STREAMER = 3
MAX_NEW_JOBS_PER_CYCLE = 10
MAX_AUTO_ATTEMPTS = 3


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid_clock")
    return value.astimezone(timezone.utc)


def _job_has_canonical_vod_url(job: Mapping[str, Any], vod_id: str) -> bool:
    urls = job.get("urls") or []
    if len(urls) == 1:
        canonical = canonical_twitch_vod_url(urls[0])
        return canonical == canonical_twitch_vod_url(vod_id)
    return False


def job_matches_vod(job: Mapping[str, Any], streamer: Any, vod_id: Any) -> bool:
    """Match durable Auto VOD metadata first, then exact canonical URL only."""
    canonical_streamer = canonical_streamer_login(streamer)
    target_id = str(vod_id or "")
    if job.get("type", "download") != "download" or not target_id.isdigit():
        return False
    if str(job.get("origin") or "") == "auto_vod":
        return (
            canonical_streamer == canonical_streamer_login(job.get("streamer"))
            and str(job.get("twitch_vod_id") or "") == target_id
        )
    return _job_has_canonical_vod_url(job, target_id)


class AutoVodCoordinator:
    """Coordinate one bounded automatic-download decision cycle; no runtime loop."""

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Dict[str, Any]],
        streamer_provider: Callable[[Dict[str, Any]], Iterable[Any]],
        state_store: AutoVodStateStore,
        job_manager: Any,
        archive_ids_provider: Callable[[Dict[str, Any]], set[str]],
        worker_target: Callable[[str], None],
        discovery: Callable[..., Dict[str, Any]] = discover_streamer_vods,
        clock: Optional[Callable[[], datetime]] = None,
        jobs_provider: Optional[Callable[[], Iterable[Mapping[str, Any]]]] = None,
        worker_starter: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._streamer_provider = streamer_provider
        self._state_store = state_store
        self._job_manager = job_manager
        self._archive_ids_provider = archive_ids_provider
        self._worker_target = worker_target
        self._discovery = discovery
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs_provider = jobs_provider or self._default_jobs
        self._worker_starter = worker_starter or self._default_worker_starter

    def _default_jobs(self) -> Iterable[Mapping[str, Any]]:
        return list(getattr(self._job_manager, "jobs", {}).values())

    def _default_worker_starter(self, job_id: str) -> Any:
        return self._job_manager.start_worker(self._worker_target, job_id)

    @staticmethod
    def _result(enabled: bool, action: str, watched: int = 0) -> Dict[str, Any]:
        return {
            "enabled": enabled,
            "state_healthy": True,
            "watched_count": watched,
            "checked_count": 0,
            "discovered_count": 0,
            "pending_count": 0,
            "queued_count": 0,
            "handled_count": 0,
            "retry_wait_count": 0,
            "error_count": 0,
            "action": action,
            "errors": [],
        }

    @staticmethod
    def _selected_streamers(settings: Dict[str, Any], values: Iterable[Any]) -> list[str]:
        profiles = normalize_streamer_profiles(settings.get("streamer_profiles"))
        selected: list[str] = []
        seen: set[str] = set()
        for value in values:
            streamer = canonical_streamer_login(value)
            if streamer and streamer not in seen:
                seen.add(streamer)
                if profiles.get(streamer, {}).get("auto_vod_download") is True:
                    selected.append(streamer)
        return selected

    @staticmethod
    def _active(job: Mapping[str, Any]) -> bool:
        return str(job.get("state") or "") in {"queued", "running", "stopping", "cancelling"}

    def _retry_or_handle(self, streamer: str, vod_id: str, record: Mapping[str, Any], reason: str, now: datetime) -> None:
        attempts = int(record.get("attempts") or 0)
        if attempts >= MAX_AUTO_ATTEMPTS:
            self._state_store.set_handled(streamer, vod_id, reason="retry_exhausted")
            return
        minutes = 60 if attempts <= 1 else 120
        retry_after = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._state_store.update_retry(streamer, vod_id, attempts=attempts, retry_after=retry_after, reason=reason)

    def _reconcile_record(self, streamer: str, vod_id: str, record: Mapping[str, Any], archive_ids: set[str], jobs: list[Mapping[str, Any]], now: datetime, errors: list[Dict[str, str]]) -> None:
        if vod_id in archive_ids:
            if record["disposition"] != "handled":
                self._state_store.set_handled(streamer, vod_id, reason="archive_present")
            return
        if record["disposition"] == "pending":
            matches = [job for job in jobs if job_matches_vod(job, streamer, vod_id) and str(job.get("origin") or "") == "auto_vod"]
            if not matches:
                return
            job = matches[-1]
            attempt = int(job.get("attempt") or record.get("attempts") or 0)
            if self._active(job):
                self._state_store.set_pending(streamer, vod_id, reason="job_interrupted", attempts=max(attempt, int(record.get("attempts") or 0)))
                self._state_store.set_queued(streamer, vod_id, str(job.get("id")), attempts=attempt)
            elif str(job.get("state")) == "completed":
                self._state_store.set_pending(streamer, vod_id, reason="job_interrupted", attempts=attempt)
                self._state_store.set_handled(streamer, vod_id, reason="downloaded")
            elif str(job.get("state")) == "cancelled":
                self._state_store.set_pending(streamer, vod_id, reason="job_interrupted", attempts=attempt)
                self._state_store.set_handled(streamer, vod_id, reason="manual_cancelled")
            elif str(job.get("state")) in {"failed", "interrupted"}:
                self._state_store.set_queued(
                    streamer, vod_id, str(job.get("id")), attempts=attempt
                )
                self._retry_or_handle(
                    streamer,
                    vod_id,
                    self._state_store.get_vod(streamer, vod_id) or record,
                    "job_failed" if str(job.get("state")) == "failed" else "job_interrupted",
                    now,
                )
            return
        if record["disposition"] != "queued":
            return
        job = next((item for item in jobs if str(item.get("id")) == str(record.get("job_id"))), None)
        if (
            job is None
            or str(job.get("origin") or "") != "auto_vod"
            or not job_matches_vod(job, streamer, vod_id)
        ):
            errors.append({"streamer": streamer, "code": "state_job_inconsistent"})
            return
        state = str(job.get("state") or "")
        if self._active(job):
            return
        if state == "completed":
            self._state_store.set_handled(streamer, vod_id, reason="downloaded")
        elif state == "cancelled":
            self._state_store.set_handled(streamer, vod_id, reason="manual_cancelled")
        elif state in {"failed", "interrupted"}:
            self._retry_or_handle(streamer, vod_id, record, "job_failed" if state == "failed" else "job_interrupted", now)

    def run_once(self) -> Dict[str, Any]:
        enabled = False
        try:
            settings = dict(self._settings_provider())
            enabled = settings.get("auto_vod_enabled") is True
            return self._run_once(settings)
        except Exception:
            result = self._result(enabled, "coordinator_error")
            result.update(
                {
                    "state_healthy": False,
                    "error_count": 1,
                    "errors": [{"code": "coordinator_error"}],
                }
            )
            return result

    def _run_once(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        if settings.get("auto_vod_enabled") is not True:
            return self._result(False, "disabled")
        selected = self._selected_streamers(settings, self._streamer_provider(settings))
        result = self._result(True, "checked", len(selected))
        if not selected:
            result["action"] = "no_streamers"
            return result
        try:
            state = self._state_store.snapshot()
            now = _utc_now(self._clock)
        except (AutoVodStateError, ValueError):
            result.update({"state_healthy": False, "action": "state_unhealthy", "error_count": 1, "errors": [{"code": "state_unhealthy"}]})
            return result
        archive_ids = {str(value) for value in self._archive_ids_provider(settings)}
        jobs = list(self._jobs_provider())
        errors: list[Dict[str, str]] = []
        for streamer in selected:
            for vod_id, record in state["streamers"].get(streamer, {"vods": {}})["vods"].items():
                try:
                    self._reconcile_record(streamer, vod_id, record, archive_ids, jobs, now, errors)
                except AutoVodStateError:
                    result.update({"state_healthy": False, "action": "state_unhealthy"})
                    return result
        try:
            state = self._state_store.snapshot()
        except AutoVodStateError:
            result.update({"state_healthy": False, "action": "state_unhealthy"})
            return result

        def discover(streamer: str) -> tuple[str, Dict[str, Any]]:
            try:
                return streamer, self._discovery(streamer, settings, limit=10)
            except Exception:
                return streamer, {"vods": [], "error": {"code": "yt_dlp_failed"}}
        with ThreadPoolExecutor(max_workers=min(MAX_DISCOVERY_WORKERS, len(selected))) as executor:
            discovered = list(executor.map(discover, selected))
        by_streamer = dict(discovered)
        created_total = 0
        stop_scheduling = False
        for streamer in selected:
            if stop_scheduling:
                break
            discovery = by_streamer[streamer]
            result["checked_count"] += 1
            if discovery.get("error"):
                errors.append({"streamer": streamer, "code": str(discovery["error"].get("code") or "yt_dlp_failed")})
            vods = [] if discovery.get("error") else list(discovery.get("vods") or [])
            result["discovered_count"] += len(vods)
            current = self._state_store.snapshot()["streamers"].get(streamer, {"vods": {}})["vods"]
            pending = [
                (vod_id, record) for vod_id, record in current.items()
                if record["disposition"] == "pending" and (not record["retry_after"] or record["retry_after"] <= now.isoformat(timespec="seconds").replace("+00:00", "Z"))
            ]
            pending.sort(key=lambda item: (item[1]["discovered_at"], item[0]))
            candidates = [vod_id for vod_id, _record in pending]
            for vod in reversed(vods):
                vod_id = str(vod.get("twitch_vod_id") or "")
                if (
                    vod_id.isdigit()
                    and 1 <= len(vod_id) <= 32
                    and vod_id not in candidates
                ):
                    candidates.append(vod_id)
            created_streamer = 0
            for vod_id in candidates:
                if created_total >= MAX_NEW_JOBS_PER_CYCLE or created_streamer >= MAX_NEW_JOBS_PER_STREAMER:
                    result["action"] = "queue_limited"
                    break
                try:
                    record = self._state_store.get_vod(streamer, vod_id)
                    if record is None:
                        record = self._state_store.ensure_pending(streamer, vod_id, reason="new_vod")
                    if record["disposition"] == "handled":
                        continue
                    if record["disposition"] == "queued":
                        continue
                    if vod_id in archive_ids:
                        self._state_store.set_handled(streamer, vod_id, reason="archive_present")
                        continue
                    matches = [job for job in jobs if job_matches_vod(job, streamer, vod_id)]
                    manual = next((job for job in matches if str(job.get("origin") or "manual") != "auto_vod"), None)
                    if manual is not None:
                        manual_state = str(manual.get("state") or "")
                        if manual_state == "completed": self._state_store.set_handled(streamer, vod_id, reason="downloaded")
                        elif manual_state == "cancelled": self._state_store.set_handled(streamer, vod_id, reason="manual_cancelled")
                        if manual_state in {"completed", "cancelled"} or self._active(manual):
                            continue
                    if record["retry_after"] and record["retry_after"] > now.isoformat(timespec="seconds").replace("+00:00", "Z"):
                        continue
                    attempt = int(record["attempts"]) + 1
                    job_id = self._job_manager.create_download_job(
                        [canonical_twitch_vod_url(vod_id)], f"Automatic Twitch VOD: {streamer}",
                        origin="auto_vod", streamer=streamer, twitch_vod_id=vod_id,
                        attempt=attempt, post_download_mode="download_only",
                    )
                    try:
                        self._state_store.set_queued(streamer, vod_id, job_id, attempts=attempt)
                    except AutoVodStateError:
                        result["action"] = "state_persistence_failed"; errors.append({"streamer": streamer, "code": "state_persistence_failed"}); stop_scheduling = True; break
                    try:
                        self._worker_starter(job_id)
                    except Exception:
                        fail_unfinished = getattr(
                            self._job_manager,
                            "fail_unfinished_download_items",
                            None,
                        )
                        if callable(fail_unfinished):
                            try:
                                fail_unfinished(job_id)
                            except Exception:
                                pass
                        result["action"] = "worker_start_failed"
                        errors.append({"streamer": streamer, "code": "worker_start_failed"})
                        stop_scheduling = True
                        break
                    jobs.append(self._job_manager.get_job(job_id) or {})
                    created_total += 1; created_streamer += 1
                    result["action"] = "queued"
                except AutoVodStateError:
                    result["action"] = "state_unhealthy"; errors.append({"streamer": streamer, "code": "state_unhealthy"}); stop_scheduling = True; break
                except Exception:
                    result["action"] = "job_persistence_failed"; errors.append({"streamer": streamer, "code": "job_persistence_failed"}); stop_scheduling = True; break
        final = self._state_store.snapshot()
        for streamer in selected:
            for record in final["streamers"].get(streamer, {"vods": {}})["vods"].values():
                result[f"{record['disposition']}_count"] += 1
                if record["disposition"] == "pending" and record.get("retry_after"):
                    result["retry_wait_count"] += 1
        result["errors"] = errors[:50]
        result["error_count"] = len(result["errors"])
        return result
