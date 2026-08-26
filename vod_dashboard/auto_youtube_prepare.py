"""Durably prepare Auto YouTube multipart manifests without creating media."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Mapping

from vod_dashboard.auto_vod_storage import GIB, assess_auto_vod_storage, required_free_bytes
from vod_dashboard.auto_youtube_multipart import (
    PART_PLAN_VERSION, MediaProbeError, MultipartPlan, plan_multipart_upload,
    probe_media,
)
from vod_dashboard.auto_youtube_plan import validate_completed_auto_youtube_source
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


SPLIT_OVERHEAD_PERCENT = 5
SPLIT_FIXED_OVERHEAD_BYTES = GIB


@dataclass(frozen=True)
class SplitStorageStatus:
    state: str
    free_bytes: int | None
    required_free_bytes: int | None

    @property
    def allows_start(self) -> bool:
        return self.state == "sufficient"


def split_required_free_bytes(total_bytes: int, source_size_bytes: int) -> int:
    """Reserve + ceil(source * 1.05) + one GiB."""
    duplicate = (int(source_size_bytes) * 105 + 99) // 100
    return required_free_bytes(int(total_bytes)) + duplicate + SPLIT_FIXED_OVERHEAD_BYTES


def assess_split_storage(media_root: Any, source_size_bytes: int, *, assessor: Callable[..., Any] = assess_auto_vod_storage) -> SplitStorageStatus:
    status = assessor(media_root)
    if status.state == "unavailable" or status.free_bytes is None or status.total_bytes is None:
        return SplitStorageStatus("unavailable", None, None)
    required = split_required_free_bytes(status.total_bytes, source_size_bytes)
    return SplitStorageStatus("sufficient" if status.free_bytes >= required else "insufficient", status.free_bytes, required)


def generation_id(record: Mapping[str, Any], plan: MultipartPlan) -> str:
    payload = {
        "algorithm": PART_PLAN_VERSION,
        "ownership": f"{record['streamer']}:{record['twitch_vod_id']}",
        "source_download_job_id": record["source_download_job_id"],
        "source_download_item_id": record["source_download_item_id"],
        "media_path": record["media_path"],
        "source_size_bytes": record["size_bytes"],
        "source_duration_seconds": plan.source_duration_seconds,
        "part_count": plan.part_count,
        "split_points_seconds": list(plan.split_points_seconds),
        "stream_signature": [list(item) for item in plan.stream_signature],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"m3v1-{digest}"


def generated_namespace(record: Mapping[str, Any], value: str) -> str:
    path = PurePosixPath(".auto-youtube", str(record["streamer"]), str(record["twitch_vod_id"]), value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid_generated_namespace")
    return path.as_posix() + "/"


class AutoYouTubePreparationService:
    """Probe, plan, storage-check, and atomically persist; never split/upload."""
    def __init__(self, *, state_store: YouTubeUploadStateStore, media_policy: MediaPathPolicy, probe: Callable[..., Any] = probe_media, storage_assessor: Callable[..., Any] = assess_auto_vod_storage, source_validator: Callable[..., Any] = validate_completed_auto_youtube_source, size_reader: Callable[[Any], int] = lambda path: path.stat().st_size) -> None:
        self._state_store = state_store
        self._media_policy = media_policy
        self._probe = probe
        self._storage_assessor = storage_assessor
        self._source_validator = source_validator
        self._size_reader = size_reader

    def _attention(self, record: Mapping[str, Any], reason: str) -> str:
        try:
            self._state_store.update_record(record["streamer"], record["twitch_vod_id"], state="needs_attention", reason=reason)
        except Exception:
            return "pending"
        return "attention"

    def _split_document(self, record: Mapping[str, Any], plan: MultipartPlan) -> Dict[str, Any]:
        value = generation_id(record, plan)
        generated_namespace(record, value)
        return {"mode": "stream_copy", "generation_id": value, "target_duration_seconds": plan.target_duration_seconds, "target_size_bytes": plan.target_size_bytes, "split_points_seconds": list(plan.split_points_seconds), "replan_count": 0}

    def _one_part(self, record: Mapping[str, Any], plan: MultipartPlan) -> Dict[str, Any]:
        return {"index": 1, "media_path": record["media_path"], "size_bytes": record["size_bytes"], "duration_seconds": plan.source_duration_seconds, "source_kind": "original", "upload_item_id": None, "upload_state": "ready", "attempts": 0, "youtube_video_id": None, "playlist_state": "pending" if record.get("playlist_id") else "not_requested", "reason": None}

    def prepare_record(self, record: Mapping[str, Any]) -> str:
        current_state = record.get("state")
        if current_state == "parts_ready":
            return "ready"
        recoverable = current_state == "needs_attention" and record.get("reason") in {"insufficient_storage", "storage_unavailable"}
        if current_state not in {"plan_ready", "parts_preparing"} and not recoverable:
            return "ignored"
        try:
            source_path = self._source_validator(record, self._media_policy)
            actual_size = self._size_reader(source_path)
            if actual_size != record.get("size_bytes"):
                raise RuntimeError("source changed")
            probe = self._probe(source_path)
            plan = plan_multipart_upload(duration_seconds=probe.duration_seconds, size_bytes=actual_size, signature=probe.streams)
            split = self._split_document(record, plan) if plan.required else None
        except MediaProbeError:
            return self._attention(record, "parts_preparation_failed")
        except Exception:
            return self._attention(record, "plan_source_invalid")

        if plan.required:
            storage = assess_split_storage(self._media_policy.media_root, actual_size, assessor=self._storage_assessor)
            target_state = "parts_preparing" if storage.allows_start else "needs_attention"
            reason = None if storage.allows_start else ("storage_unavailable" if storage.state == "unavailable" else "insufficient_storage")
            try:
                self._state_store.set_preparation(record["streamer"], record["twitch_vod_id"], source_duration_seconds=plan.source_duration_seconds, state=target_state, split=split, parts=[], reason=reason)
            except Exception:
                return "pending"
            return "preparing" if storage.allows_start else "blocked"

        try:
            self._state_store.set_preparation(record["streamer"], record["twitch_vod_id"], source_duration_seconds=plan.source_duration_seconds, state="parts_ready", split=None, parts=[self._one_part(record, plan)], reason=None)
        except Exception:
            return "pending"
        return "ready"

    def reconcile(self) -> Dict[str, int]:
        result = {"ready": 0, "preparing": 0, "blocked": 0, "attention": 0, "pending": 0, "ignored": 0}
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.prepare_record(record)
            result[outcome] += 1
        return result
