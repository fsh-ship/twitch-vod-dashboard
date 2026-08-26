"""Durably materialize deferred Auto YouTube jobs without executing uploads."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from vod_dashboard.auto_youtube_plan import validate_completed_auto_youtube_source
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStateError,
    YouTubeUploadStateStore,
    canonical_upload_key,
    validate_upload_plan,
)


class AutoYouTubeMaterializationService:
    """Idempotently join a plan-ready owner to one deferred JobStore job."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        job_manager: Any,
        media_policy: MediaPathPolicy,
    ) -> None:
        self._state_store = state_store
        self._job_manager = job_manager
        self._media_policy = media_policy

    @staticmethod
    def _source(record: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "streamer",
                "twitch_vod_id",
                "source_download_job_id",
                "source_download_item_id",
                "media_path",
                "size_bytes",
            )
        }

    @staticmethod
    def _matches(
        job: Mapping[str, Any], record: Mapping[str, Any], key: str
    ) -> bool:
        context = job.get("auto_youtube_context")
        metadata = job.get("item_metadata")
        plan = record.get("upload_plan")
        if not isinstance(context, Mapping) or not isinstance(metadata, list):
            return False
        if len(metadata) != 1 or not isinstance(metadata[0], Mapping):
            return False
        return (
            job.get("type") == "youtube_upload"
            and job.get("origin") == "auto_youtube"
            and job.get("execution_deferred") is True
            and job.get("auto_youtube_key") == key
            and all(
                context.get(name) == record.get(name)
                for name in (
                    "streamer",
                    "twitch_vod_id",
                    "source_download_job_id",
                    "source_download_item_id",
                    "media_path",
                )
            )
            and job.get("urls") == [record.get("media_path")]
            and job.get("playlist_id", "")
            == str(record.get("playlist_id") or "")
            and metadata[0].get("streamer") == record.get("streamer")
            and metadata[0].get("vod_id") == record.get("twitch_vod_id")
            and metadata[0].get("size_bytes") == record.get("size_bytes")
            and metadata[0].get("youtube_playlist_id", "")
            == str(record.get("playlist_id") or "")
            and isinstance(plan, Mapping)
            and metadata[0].get("title") == plan.get("title")
        )

    def _attention(self, record: Mapping[str, Any], reason: str) -> str:
        try:
            self._state_store.update_record(
                record["streamer"],
                record["twitch_vod_id"],
                state="needs_attention",
                reason=reason,
            )
        except Exception:
            return "pending"
        return "attention"

    def _job_store_is_healthy(self) -> bool:
        try:
            health = self._job_manager.persistence_status()
        except Exception:
            return False
        return health.get("enabled") is True and health.get("healthy") is True

    def _matching_jobs(
        self, record: Mapping[str, Any], key: str
    ) -> tuple[list[Mapping[str, Any]], bool]:
        matching: list[Mapping[str, Any]] = []
        conflict = False
        for job in self._job_manager.snapshot_jobs():
            if job.get("origin") != "auto_youtube":
                continue
            if job.get("auto_youtube_key") != key:
                continue
            if self._matches(job, record, key):
                matching.append(job)
            else:
                conflict = True
        return matching, conflict

    def _attach(self, record: Mapping[str, Any], job_id: str) -> str:
        try:
            self._state_store.update_record(
                record["streamer"],
                record["twitch_vod_id"],
                state="upload_queued",
                upload_job_id=job_id,
                reason=None,
            )
        except YouTubeUploadStateError:
            # The durable JobStore job has the canonical key. A later
            # reconciliation deterministically attaches it without cloning.
            return "pending"
        except Exception:
            return "pending"
        return "queued"

    def materialize_record(self, record: Mapping[str, Any]) -> str:
        state = record.get("state")
        if state not in {"plan_ready", "upload_queued"}:
            return "ignored"
        try:
            key = canonical_upload_key(
                record.get("streamer"), record.get("twitch_vod_id")
            )
            plan = validate_upload_plan(record.get("upload_plan"))
        except Exception:
            return self._attention(record, "materialization_consistency_error")

        if not self._job_store_is_healthy():
            return "pending"
        matching, conflict = self._matching_jobs(record, key)
        if conflict or len(matching) > 1:
            return self._attention(record, "materialization_consistency_error")

        if state == "upload_queued":
            expected_id = str(record.get("upload_job_id") or "")
            if len(matching) == 1 and str(matching[0].get("id") or "") == expected_id:
                return "queued"
            return self._attention(record, "materialization_consistency_error")

        # A plan-ready record must still describe the exact P8c media result.
        try:
            source_path = self._media_policy.resolve_media_path(
                record.get("media_path"), must_exist=False
            )
            if not source_path.exists():
                return self._attention(record, "materialization_media_missing")
            validate_completed_auto_youtube_source(record, self._media_policy)
        except Exception:
            return self._attention(record, "materialization_source_invalid")

        if len(matching) == 1:
            return self._attach(record, str(matching[0]["id"]))
        if record.get("upload_job_id") is not None:
            return self._attention(record, "materialization_consistency_error")
        try:
            job_id = self._job_manager.create_auto_youtube_upload_job_deferred(
                source=self._source(record),
                upload_plan=plan,
                playlist_id=str(record.get("playlist_id") or ""),
            )
        except Exception:
            return "pending"
        return self._attach(record, str(job_id))

    def reconcile(self) -> Dict[str, int]:
        """Materialize only existing plans; never execute an upload or API call."""
        result = {"queued": 0, "attention": 0, "pending": 0, "ignored": 0}
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.materialize_record(record)
            result[outcome] = result.get(outcome, 0) + 1
        return result
