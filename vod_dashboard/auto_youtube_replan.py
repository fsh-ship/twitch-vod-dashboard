"""Bounded deterministic replanning for proven-invalid multipart generations."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Callable, Dict, Mapping

from vod_dashboard.auto_youtube_multipart import (
    PART_PLAN_VERSION,
    TARGET_DURATION_SECONDS,
    TARGET_SIZE_BYTES,
    MediaProbeResult,
    MultipartPlan,
    deterministic_split_points,
    probe_media,
    stream_signature,
)
from vod_dashboard.auto_youtube_plan import validate_completed_auto_youtube_source
from vod_dashboard.auto_youtube_prepare import generation_id, generated_namespace
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    MAX_AUTOMATIC_REPLANS,
    YouTubeUploadStateStore,
)


SOURCE_DURATION_TOLERANCE_SECONDS = 1.0


class MultipartReplanError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def authorized_generation_root(
    record: Mapping[str, Any], media_policy: MediaPathPolicy
) -> Path:
    """Resolve only the exact lexical generation root and reject link escapes."""
    split = record.get("split")
    if not isinstance(split, Mapping):
        raise MultipartReplanError("multipart_replan_unsafe")
    relative = generated_namespace(record, str(split.get("generation_id") or ""))
    root = media_policy.media_root.resolve()
    expected = root.joinpath(*PurePosixPath(relative).parts)
    resolved = media_policy.resolve_media_path(relative, must_exist=False)
    if resolved != expected or resolved == root or root not in resolved.parents:
        raise MultipartReplanError("multipart_replan_unsafe")
    return resolved


def _multipart_plan(
    record: Mapping[str, Any], probe: MediaProbeResult, part_count: int
) -> MultipartPlan:
    duration = float(record["source_duration_seconds"])
    points = deterministic_split_points(duration, part_count)
    return MultipartPlan(
        required=True,
        source_duration_seconds=duration,
        source_size_bytes=int(record["size_bytes"]),
        part_count=part_count,
        split_points_seconds=points,
        target_duration_seconds=TARGET_DURATION_SECONDS,
        target_size_bytes=TARGET_SIZE_BYTES,
        stream_signature=stream_signature(probe.streams),
    )


class AutoYouTubeReplanService:
    """Replace at most one invalid generation plan; never split or upload."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        media_policy: MediaPathPolicy,
        probe: Callable[[Path], MediaProbeResult] = probe_media,
        source_validator: Callable[..., Path] = validate_completed_auto_youtube_source,
        rmtree: Callable[..., Any] = shutil.rmtree,
    ) -> None:
        self._state_store = state_store
        self._media_policy = media_policy
        self._probe = probe
        self._source_validator = source_validator
        self._rmtree = rmtree

    def _attention(self, record: Mapping[str, Any], reason: str) -> str:
        try:
            self._state_store.update_record(
                record["streamer"], record["twitch_vod_id"],
                state="needs_attention", reason=reason,
            )
        except Exception:
            return "pending"
        return "exhausted" if reason == "multipart_replan_exhausted" else "attention"

    @staticmethod
    def _eligible_shape(record: Mapping[str, Any]) -> bool:
        return (
            record.get("state") == "needs_attention"
            and record.get("reason") == "multipart_replan_required"
            and record.get("upload_job_id") is None
            and record.get("part_plan_version") == PART_PLAN_VERSION
            and isinstance(record.get("upload_plan"), Mapping)
            and record.get("parts") == []
        )

    def _replacement(
        self, record: Mapping[str, Any], source_probe: MediaProbeResult
    ) -> tuple[str, Dict[str, Any], Path]:
        split = record.get("split")
        if not isinstance(split, Mapping):
            raise MultipartReplanError("multipart_replan_unsafe")
        required_fields = {
            "mode", "generation_id", "target_duration_seconds",
            "target_size_bytes", "split_points_seconds", "replan_count",
        }
        if set(split) != required_fields or split.get("mode") != "stream_copy":
            raise MultipartReplanError("multipart_replan_unsafe")
        current_points = split.get("split_points_seconds")
        replan_count = split.get("replan_count")
        if (
            not isinstance(current_points, list)
            or not current_points
            or isinstance(replan_count, bool)
            or not isinstance(replan_count, int)
            or not 0 <= replan_count <= MAX_AUTOMATIC_REPLANS
            or split.get("target_duration_seconds") != TARGET_DURATION_SECONDS
            or split.get("target_size_bytes") != TARGET_SIZE_BYTES
        ):
            raise MultipartReplanError("multipart_replan_unsafe")
        if replan_count >= MAX_AUTOMATIC_REPLANS:
            raise MultipartReplanError("multipart_replan_exhausted")

        duration = record.get("source_duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration <= 0
            or not source_probe.streams
            or abs(source_probe.duration_seconds - float(duration))
            > SOURCE_DURATION_TOLERANCE_SECONDS
        ):
            raise MultipartReplanError("multipart_replan_source_invalid")

        current_count = len(current_points) + 1
        current_plan = _multipart_plan(record, source_probe, current_count)
        if list(current_plan.split_points_seconds) != current_points:
            raise MultipartReplanError("multipart_replan_unsafe")
        current_generation = generation_id(record, current_plan)
        if current_generation != split.get("generation_id"):
            raise MultipartReplanError("multipart_replan_unsafe")

        next_plan = _multipart_plan(record, source_probe, current_count + 1)
        next_generation = generation_id(record, next_plan)
        if next_generation == current_generation:
            raise MultipartReplanError("multipart_replan_unsafe")
        replacement = {
            "mode": "stream_copy",
            "generation_id": next_generation,
            "target_duration_seconds": next_plan.target_duration_seconds,
            "target_size_bytes": next_plan.target_size_bytes,
            "split_points_seconds": list(next_plan.split_points_seconds),
            "replan_count": replan_count + 1,
        }
        return current_generation, replacement, authorized_generation_root(
            record, self._media_policy
        )

    def replan_record(self, record: Mapping[str, Any]) -> str:
        if record.get("state") != "needs_attention" or record.get("reason") != "multipart_replan_required":
            return "ignored"
        if not self._eligible_shape(record):
            return self._attention(record, "multipart_replan_unsafe")
        try:
            source = self._source_validator(record, self._media_policy)
            source_probe = self._probe(source)
        except Exception:
            return self._attention(record, "multipart_replan_source_invalid")
        try:
            current_generation, replacement, generation_root = self._replacement(
                record, source_probe
            )
        except MultipartReplanError as exc:
            return self._attention(record, exc.code)
        except Exception:
            return self._attention(record, "multipart_replan_unsafe")
        source = Path(source).resolve()
        if source == generation_root or generation_root in source.parents:
            return self._attention(record, "multipart_replan_unsafe")
        try:
            current = self._state_store.get(
                record["streamer"], record["twitch_vod_id"]
            )
        except Exception:
            return "pending"
        if current != dict(record):
            return "ignored"
        try:
            if generation_root.exists():
                if not generation_root.is_dir() or generation_root.is_symlink():
                    raise MultipartReplanError("multipart_replan_unsafe")
                if generation_root.stat().st_dev != self._media_policy.media_root.resolve().stat().st_dev:
                    raise MultipartReplanError("multipart_replan_unsafe")
                self._rmtree(generation_root)
        except MultipartReplanError as exc:
            return self._attention(record, exc.code)
        except Exception:
            return self._attention(record, "multipart_replan_failed")
        try:
            self._state_store.replace_split_for_replan(
                record["streamer"], record["twitch_vod_id"],
                expected_generation_id=current_generation, split=replacement,
            )
        except Exception:
            return "pending"
        return "replanned"

    def reconcile(self) -> Dict[str, int]:
        result = {
            "replanned": 0, "exhausted": 0, "attention": 0,
            "pending": 0, "ignored": 0,
        }
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.replan_record(record)
            result[outcome] += 1
        return result
