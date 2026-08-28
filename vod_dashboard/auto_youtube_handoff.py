"""Durable, side-effect-free Auto VOD to Auto YouTube intent handoff."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from vod_dashboard.settings import canonical_streamer_login, normalize_streamer_profiles
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStateError,
    YouTubeUploadStateLoadError,
    YouTubeUploadStateStore,
)


@dataclass(frozen=True)
class AutoYouTubeAdmission:
    handoff: str
    reason: str
    playlist_id: str
    execution_policy: str


def completion_admission(
    settings: Mapping[str, Any], streamer: Any
) -> AutoYouTubeAdmission:
    """Freeze the strict opt-in decision from settings at completion time."""
    canonical_streamer = canonical_streamer_login(streamer)
    if settings.get("auto_youtube_enabled") is not True:
        return AutoYouTubeAdmission("not_eligible", "global_disabled", "", "manual")
    profile = normalize_streamer_profiles(settings.get("streamer_profiles")).get(
        canonical_streamer, {}
    )
    if profile.get("auto_youtube_upload") is not True:
        return AutoYouTubeAdmission("not_eligible", "streamer_disabled", "", "manual")
    return AutoYouTubeAdmission(
        "intent_pending", "", str(profile.get("youtube_playlist_id") or "").strip(),
        "automatic",
    )


def _completed_source(job: Mapping[str, Any], item_id: str) -> Optional[Dict[str, Any]]:
    """Return only a complete, already-persisted single-item Auto VOD result."""
    if (
        job.get("origin") != "auto_vod"
        or job.get("item_states") != ["completed"]
        or list(job.get("item_ids") or []) != [str(item_id)]
    ):
        return None
    streamer = canonical_streamer_login(job.get("streamer"))
    vod_id = str(job.get("twitch_vod_id") or "")
    path = job.get("completed_media_path")
    size = job.get("completed_media_size_bytes")
    completed_vod_id = job.get("completed_twitch_vod_id")
    if (
        not streamer
        or not vod_id.isdigit()
        or completed_vod_id != vod_id
        or not isinstance(path, str)
        or not path
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        return None
    return {
        "streamer": streamer,
        "twitch_vod_id": vod_id,
        "source_download_job_id": str(job.get("id") or ""),
        "source_download_item_id": str(item_id),
        "media_path": path,
        "size_bytes": size,
    }


def _matching_owner(record: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
    return all(
        record.get(key) == source.get(key)
        for key in (
            "streamer",
            "twitch_vod_id",
            "source_download_job_id",
            "source_download_item_id",
            "media_path",
            "size_bytes",
        )
    )


class AutoYouTubeHandoffService:
    """Create/reconcile ownership intents; never starts or prepares uploads."""

    def __init__(self, *, job_manager: Any, state_store: YouTubeUploadStateStore) -> None:
        self._job_manager = job_manager
        self._state_store = state_store

    @staticmethod
    def _handoff(job: Mapping[str, Any]) -> str:
        values = job.get("item_auto_youtube_handoffs") or []
        return values[0] if isinstance(values, list) and len(values) == 1 else ""

    @staticmethod
    def _playlist_id(job: Mapping[str, Any]) -> str:
        values = job.get("item_auto_youtube_playlist_ids") or []
        return str(values[0] or "") if isinstance(values, list) and len(values) == 1 else ""

    @staticmethod
    def _execution_policy(job: Mapping[str, Any]) -> str:
        values = job.get("item_auto_youtube_execution_policies") or []
        return (
            str(values[0])
            if isinstance(values, list) and len(values) == 1
            else "manual"
        )

    def _block(self, job_id: str, item_id: str, reason: str) -> str:
        try:
            self._job_manager.set_auto_youtube_handoff(
                job_id, item_id, "handoff_blocked", reason=reason
            )
        except Exception:
            return "pending"
        return "blocked"

    def admit_pending(
        self, job_id: str, item_id: str, *, plan_inputs: Any = None
    ) -> str:
        """Claim a durable pending intent after source completion persistence."""
        job = self._job_manager.get_job(job_id) or {}
        source = _completed_source(job, item_id)
        if source is None:
            return self._block(job_id, item_id, "invalid_completed_result")
        if self._handoff(job) != "intent_pending":
            return "ignored"
        try:
            health = self._state_store.health()
        except Exception:
            health = {"healthy": False}
        if health.get("healthy") is not True:
            return self._block(job_id, item_id, "upload_state_unhealthy")
        try:
            record, _created = self._state_store.create_intent_if_absent(
                **source,
                playlist_id=self._playlist_id(job) or None,
                plan_inputs=plan_inputs,
                execution_policy=self._execution_policy(job),
            )
        except YouTubeUploadStateLoadError:
            return self._block(job_id, item_id, "upload_state_unhealthy")
        except YouTubeUploadStateError:
            # The source remains durably pending so a healthy later restart
            # can retry ownership creation without reinterpreting settings.
            return "pending"
        except Exception:
            return "pending"
        if not _matching_owner(record, source):
            return self._block(job_id, item_id, "intent_conflict")
        try:
            self._job_manager.set_auto_youtube_handoff(
                job_id, item_id, "intent_created"
            )
        except Exception:
            # The ledger is authoritative and the pending source marker is
            # intentionally recoverable by reconciliation.
            return "pending"
        return "created"

    def reconcile(self) -> Dict[str, int]:
        """Finish only prior completion-time pending decisions; never backfill."""
        result = {"created": 0, "blocked": 0, "pending": 0, "ignored": 0}
        for job in self._job_manager.snapshot_jobs():
            if job.get("origin") != "auto_vod":
                continue
            item_ids = job.get("item_ids") or []
            if len(item_ids) != 1:
                continue
            handoff = self._handoff(job)
            item_id = str(item_ids[0])
            if handoff == "intent_pending":
                outcome = self.admit_pending(str(job.get("id") or ""), item_id)
            elif handoff == "intent_created":
                source = _completed_source(job, item_id)
                if source is None:
                    outcome = self._block(
                        str(job.get("id") or ""), item_id, "invalid_completed_result"
                    )
                else:
                    try:
                        health = self._state_store.health()
                    except Exception:
                        health = {"healthy": False}
                    if health.get("healthy") is not True:
                        outcome = self._block(
                            str(job.get("id") or ""),
                            item_id,
                            "upload_state_unhealthy",
                        )
                    else:
                        try:
                            existing = self._state_store.get(
                                source["streamer"], source["twitch_vod_id"]
                            )
                        except YouTubeUploadStateError:
                            existing = None
                        outcome = (
                            "ignored"
                            if existing is not None and _matching_owner(existing, source)
                            else self._block(
                                str(job.get("id") or ""), item_id, "intent_missing"
                            )
                        )
            else:
                outcome = "ignored"
            result[outcome] = result.get(outcome, 0) + 1
        return result
