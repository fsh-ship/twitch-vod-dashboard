"""Safe public projection for the compact Auto VOD monitor status."""

from __future__ import annotations

from typing import Any, Dict, Mapping


GIB = 1024 ** 3
STORAGE_STATES = frozenset({"not_checked", "sufficient", "insufficient", "unavailable"})
PUBLIC_ACTIONS = frozenset(
    {
        "disabled",
        "no_streamers",
        "checked",
        "queued",
        "waiting_for_existing_job",
        "rearmed_storage_blocked_job",
        "storage_insufficient",
        "storage_unavailable",
        "migration_required",
        "state_unhealthy",
        "shutdown_requested",
        "queue_limited",
        "job_persistence_failed",
        "state_persistence_failed",
        "worker_start_failed",
        "coordinator_error",
        "monitor_error",
    }
)
COUNT_FIELDS = (
    "checked_count",
    "discovered_count",
    "queued_count",
    "handled_count",
    "retry_wait_count",
    "error_count",
    "storage_blocked_count",
    "outstanding_auto_vod_jobs",
    "baseline_established_count",
    "baseline_initialized_count",
    "baseline_pending_count",
)


def _safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _gib(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return round(float(value) / GIB, 1)


def public_auto_vod_status(
    snapshot: Mapping[str, Any],
    *,
    initialized: bool,
    enabled: bool,
    watched_count: int,
    poll_minutes: int,
) -> Dict[str, Any]:
    """Project monitor data without paths, raw errors, VOD IDs, or raw bytes."""
    raw_result = snapshot.get("last_result")
    safe_result: Dict[str, Any] | None = None
    if isinstance(raw_result, Mapping):
        action = raw_result.get("action")
        storage_state = raw_result.get("storage_state")
        safe_result = {
            key: _safe_count(raw_result.get(key)) for key in COUNT_FIELDS
        }
        safe_result["action"] = action if action in PUBLIC_ACTIONS else "checked"
        safe_result["enabled"] = raw_result.get("enabled") is True
        safe_result["state_healthy"] = raw_result.get("state_healthy") is not False
        safe_result["storage_state"] = (
            storage_state if storage_state in STORAGE_STATES else "not_checked"
        )
        safe_result["storage_free_gb"] = _gib(raw_result.get("storage_free_bytes"))
        safe_result["storage_required_gb"] = _gib(
            raw_result.get("storage_required_bytes")
        )

    return {
        "initialized": initialized is True,
        "enabled": enabled is True,
        "poll_minutes": poll_minutes if poll_minutes in {60, 120} else 60,
        "running": snapshot.get("running") is True,
        "thread_alive": snapshot.get("thread_alive") is True,
        "in_progress": snapshot.get("in_progress") is True,
        "last_started_at": snapshot.get("last_started_at"),
        "last_finished_at": snapshot.get("last_finished_at"),
        "next_check_at": snapshot.get("next_check_at"),
        "last_result": safe_result,
        "watched_count": _safe_count(watched_count),
    }
