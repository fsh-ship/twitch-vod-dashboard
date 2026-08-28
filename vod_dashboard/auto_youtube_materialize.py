"""Durably materialize deferred Auto YouTube bundles without executing uploads."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from vod_dashboard.auto_youtube_multipart import (
    MediaProbeResult,
    derive_part_upload_plan,
    generated_part_within_limits,
    original_part_within_limits,
    probe_media,
    stream_signatures_match,
)
from vod_dashboard.auto_youtube_plan import validate_completed_auto_youtube_source
from vod_dashboard.media import MediaPathPolicy, VIDEO_EXTENSIONS
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStateError,
    YouTubeUploadStateStore,
    canonical_upload_key,
    validate_upload_plan,
)


PART_DURATION_TOLERANCE_SECONDS = 1.0


class _MissingMaterializationMedia(RuntimeError):
    pass


class _InvalidMaterializationMedia(RuntimeError):
    pass


class AutoYouTubeMaterializationService:
    """Idempotently join one finalized ownership bundle to one deferred job."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        job_manager: Any,
        media_policy: MediaPathPolicy,
        probe: Callable[[Path], MediaProbeResult] = probe_media,
    ) -> None:
        self._state_store = state_store
        self._job_manager = job_manager
        self._media_policy = media_policy
        self._probe = probe

    @staticmethod
    def _source(record: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "streamer", "twitch_vod_id", "source_download_job_id",
                "source_download_item_id", "media_path",
            )
        }

    @staticmethod
    def _part_descriptors(record: Mapping[str, Any]) -> list[Dict[str, Any]]:
        parts = record.get("parts")
        if not isinstance(parts, list) or not parts:
            raise _InvalidMaterializationMedia("missing manifest")
        total = len(parts)
        descriptors: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for index, part in enumerate(parts, 1):
            if (
                not isinstance(part, Mapping)
                or part.get("index") != index
                or part.get("media_path") in seen
                or part.get("source_kind") not in {"original", "generated"}
            ):
                raise _InvalidMaterializationMedia("invalid manifest")
            path = str(part["media_path"])
            seen.add(path)
            descriptors.append({
                "index": index,
                "total": total,
                "media_path": path,
                "size_bytes": part.get("size_bytes"),
                "duration_seconds": part.get("duration_seconds"),
                "source_kind": part.get("source_kind"),
            })
        if total == 1:
            only = descriptors[0]
            if (
                only["source_kind"] != "original"
                or only["media_path"] != record.get("media_path")
                or only["size_bytes"] != record.get("size_bytes")
                or only["duration_seconds"] != record.get("source_duration_seconds")
            ):
                raise _InvalidMaterializationMedia("invalid original manifest")
        elif any(part["source_kind"] != "generated" for part in descriptors):
            raise _InvalidMaterializationMedia("mixed manifest")
        return descriptors

    @staticmethod
    def _metadata(
        record: Mapping[str, Any], plan: Mapping[str, Any],
        descriptors: list[Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        return [
            {
                "streamer": record["streamer"],
                "date": "",
                "title": derive_part_upload_plan(
                    plan, index=index, total=len(descriptors)
                )["title"],
                "vod_id": record["twitch_vod_id"],
                "name": Path(str(part["media_path"])).name,
                "size_bytes": part["size_bytes"],
                "size_gb": None,
                "youtube_playlist_id": str(record.get("playlist_id") or ""),
            }
            for index, part in enumerate(descriptors, 1)
        ]

    def _validate_media(
        self, record: Mapping[str, Any], descriptors: list[Mapping[str, Any]]
    ) -> None:
        if len(descriptors) == 1:
            try:
                source_path = self._media_policy.resolve_media_path(
                    record.get("media_path"), must_exist=False
                )
                if not source_path.exists():
                    raise _MissingMaterializationMedia()
                validate_completed_auto_youtube_source(record, self._media_policy)
            except _MissingMaterializationMedia:
                raise
            except FileNotFoundError as exc:
                raise _MissingMaterializationMedia() from exc
            except Exception as exc:
                raise _InvalidMaterializationMedia() from exc
        generated_streams = None
        for descriptor in descriptors:
            try:
                path = self._media_policy.resolve_media_path(
                    descriptor["media_path"], must_exist=False
                )
                if not path.exists():
                    raise _MissingMaterializationMedia()
                path = self._media_policy.resolve_media_path(
                    descriptor["media_path"], must_exist=True, require_file=True,
                    allowed_extensions=VIDEO_EXTENSIONS,
                )
            except _MissingMaterializationMedia:
                raise
            except Exception as exc:
                raise _InvalidMaterializationMedia() from exc
            size = path.stat().st_size
            if size <= 0 or size != descriptor["size_bytes"]:
                raise _InvalidMaterializationMedia()
            try:
                measured = self._probe(path)
            except Exception as exc:
                raise _InvalidMaterializationMedia() from exc
            try:
                duration_delta = abs(
                    measured.duration_seconds - descriptor["duration_seconds"]
                )
            except (TypeError, ValueError) as exc:
                raise _InvalidMaterializationMedia() from exc
            if duration_delta > PART_DURATION_TOLERANCE_SECONDS:
                raise _InvalidMaterializationMedia()
            if descriptor["source_kind"] == "original":
                if not original_part_within_limits(
                    duration_seconds=measured.duration_seconds, size_bytes=size
                ):
                    raise _InvalidMaterializationMedia()
            else:
                if not generated_part_within_limits(
                    duration_seconds=measured.duration_seconds, size_bytes=size
                ):
                    raise _InvalidMaterializationMedia()
                if generated_streams is None:
                    generated_streams = measured.streams
                elif not stream_signatures_match(generated_streams, measured.streams):
                    raise _InvalidMaterializationMedia()

    @staticmethod
    def _matches(
        job: Mapping[str, Any], record: Mapping[str, Any], key: str,
        descriptors: list[Mapping[str, Any]], metadata: list[Mapping[str, Any]],
    ) -> bool:
        context = job.get("auto_youtube_context")
        item_ids = job.get("item_ids")
        if not isinstance(context, Mapping) or not isinstance(item_ids, list):
            return False
        expected_context = AutoYouTubeMaterializationService._source(record)
        if (
            job.get("type") != "youtube_upload"
            or job.get("origin") != "auto_youtube"
            or not isinstance(job.get("execution_deferred"), bool)
            or job.get("auto_youtube_key") != key
            or job.get("auto_youtube_execution_policy", "manual")
            != record.get("execution_policy")
            or dict(context) != expected_context
            or job.get("urls") != [part["media_path"] for part in descriptors]
            or job.get("playlist_id", "") != str(record.get("playlist_id") or "")
            or job.get("item_metadata") != metadata
            or len(item_ids) != len(descriptors)
            or len(item_ids) != len(set(item_ids))
        ):
            return False
        stored_parts = job.get("auto_youtube_parts")
        if stored_parts is None:
            # P8f's old persisted shape was valid only for a one-part original.
            return len(descriptors) == 1 and descriptors[0]["source_kind"] == "original"
        return stored_parts == descriptors

    def _attention(self, record: Mapping[str, Any], reason: str) -> str:
        try:
            self._state_store.update_record(
                record["streamer"], record["twitch_vod_id"],
                state="needs_attention", reason=reason,
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
        self, record: Mapping[str, Any], key: str,
        descriptors: list[Mapping[str, Any]], metadata: list[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], bool]:
        matching: list[Mapping[str, Any]] = []
        conflict = False
        for job in self._job_manager.snapshot_jobs():
            if job.get("origin") != "auto_youtube":
                continue
            if job.get("auto_youtube_key") != key:
                continue
            if self._matches(job, record, key, descriptors, metadata):
                matching.append(job)
            else:
                conflict = True
        return matching, conflict

    def _attach(self, record: Mapping[str, Any], job: Mapping[str, Any]) -> str:
        item_ids = job.get("item_ids")
        if not isinstance(item_ids, list):
            return self._attention(record, "materialization_consistency_error")
        try:
            self._state_store.attach_materialized_upload(
                record["streamer"], record["twitch_vod_id"],
                upload_job_id=str(job.get("id") or ""), upload_item_ids=item_ids,
            )
        except YouTubeUploadStateError:
            # JobStore admitted the immutable deferred bundle first. A later
            # reconciliation can link the same durable item IDs without cloning.
            return "pending"
        except Exception:
            return "pending"
        return "queued"

    def materialize_record(self, record: Mapping[str, Any]) -> str:
        state = record.get("state")
        if state not in {"parts_ready", "upload_queued"}:
            return "ignored"
        try:
            key = canonical_upload_key(
                record.get("streamer"), record.get("twitch_vod_id")
            )
            plan = validate_upload_plan(record.get("upload_plan"))
            descriptors = self._part_descriptors(record)
            metadata = self._metadata(record, plan, descriptors)
        except Exception:
            return self._attention(record, "materialization_consistency_error")

        if not self._job_store_is_healthy():
            return "pending"
        if state == "parts_ready":
            try:
                self._validate_media(record, descriptors)
            except _MissingMaterializationMedia:
                return self._attention(record, "materialization_media_missing")
            except _InvalidMaterializationMedia:
                return self._attention(record, "materialization_source_invalid")
        matching, conflict = self._matching_jobs(record, key, descriptors, metadata)
        if conflict or len(matching) > 1:
            return self._attention(record, "materialization_consistency_error")

        if state == "upload_queued":
            expected_id = str(record.get("upload_job_id") or "")
            if len(matching) == 1 and str(matching[0].get("id") or "") == expected_id:
                return "queued"
            return self._attention(record, "materialization_consistency_error")

        if len(matching) == 1:
            return self._attach(record, matching[0])
        if record.get("upload_job_id") is not None:
            return self._attention(record, "materialization_consistency_error")
        try:
            job_id = self._job_manager.create_auto_youtube_upload_job_deferred(
                source=self._source(record), upload_plan=plan,
                playlist_id=str(record.get("playlist_id") or ""),
                parts=descriptors,
                execution_policy=str(record.get("execution_policy") or "manual"),
            )
            job = self._job_manager.get_job(job_id)
        except Exception:
            return "pending"
        if not isinstance(job, Mapping) or not self._matches(
            job, record, key, descriptors, metadata
        ):
            return self._attention(record, "materialization_consistency_error")
        return self._attach(record, job)

    def reconcile(self) -> Dict[str, int]:
        """Materialize only finalized bundles; never execute an upload or API call."""
        result = {"queued": 0, "attention": 0, "pending": 0, "ignored": 0}
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.materialize_record(record)
            result[outcome] = result.get(outcome, 0) + 1
        return result
