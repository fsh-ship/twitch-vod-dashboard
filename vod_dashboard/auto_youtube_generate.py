"""Crash-safe stream-copy generation for an already authorized split plan."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from vod_dashboard.auto_vod_storage import assess_auto_vod_storage, required_free_bytes
from vod_dashboard.auto_youtube_multipart import (
    MediaProbeError, MediaProbeResult, generated_part_within_limits, probe_media,
    stream_signatures_match,
)
from vod_dashboard.auto_youtube_plan import validate_completed_auto_youtube_source
from vod_dashboard.auto_youtube_prepare import assess_split_storage, generated_namespace
from vod_dashboard.media import MediaPathPolicy, VIDEO_EXTENSIONS
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


RUNTIME_STORAGE_POLL_SECONDS = 5
AGGREGATE_BASE_TOLERANCE_SECONDS = 10.0
AGGREGATE_PER_PART_TOLERANCE_SECONDS = 5.0


class MultipartGenerationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GenerationPaths:
    root: Path
    staging: Path
    final: Path
    relative_root: str


def generation_paths(record: Mapping[str, Any], media_policy: MediaPathPolicy) -> GenerationPaths:
    split = record.get("split")
    if not isinstance(split, Mapping):
        raise MultipartGenerationError("multipart_validation_failed")
    relative = generated_namespace(record, str(split.get("generation_id") or ""))
    root = media_policy.resolve_media_path(relative, must_exist=False)
    staging = media_policy.resolve_media_path(relative + "staging", must_exist=False)
    final = media_policy.resolve_media_path(relative + "parts", must_exist=False)
    return GenerationPaths(root, staging, final, relative)


def expected_part_names(count: int, extension: str) -> tuple[str, ...]:
    if count < 2 or extension.lower() not in VIDEO_EXTENSIONS:
        raise MultipartGenerationError("multipart_validation_failed")
    suffix = extension.lower()
    return tuple(f"part-{index:03d}-of-{count:03d}{suffix}" for index in range(1, count + 1))


def build_ffmpeg_command(source: Path, staging: Path, split_points: Sequence[float], count: int, *, ffmpeg_binary: str = "ffmpeg") -> list[str]:
    names = expected_part_names(count, source.suffix)
    pattern = staging / names[0].replace("001", "%03d", 1)
    times = ",".join(format(float(point), ".3f").rstrip("0").rstrip(".") for point in split_points)
    return [
        ffmpeg_binary, "-hide_banner", "-nostdin", "-i", str(source),
        "-map", "0", "-c", "copy", "-f", "segment",
        "-segment_times", times, "-segment_start_number", "1",
        "-reset_timestamps", "1", str(pattern),
    ]


def _terminate(process: Any) -> None:
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill(); process.wait(timeout=5)
        except Exception:
            pass


def run_ffmpeg_segmentation(
    command: Sequence[str], *, media_root: Path,
    popen: Callable[..., Any] = subprocess.Popen,
    storage_assessor: Callable[..., Any] = assess_auto_vod_storage,
    cancel_requested: Callable[[], bool] = lambda: False,
    poll_seconds: float = RUNTIME_STORAGE_POLL_SECONDS,
) -> None:
    """Run with bounded output and terminate before reserve is breached."""
    try:
        process = popen(
            list(command), shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MultipartGenerationError("ffmpeg_unavailable") from exc
    while True:
        if cancel_requested():
            _terminate(process)
            raise MultipartGenerationError("multipart_generation_incomplete")
        status = storage_assessor(media_root)
        if status.state == "unavailable" or status.free_bytes is None or status.total_bytes is None:
            _terminate(process)
            raise MultipartGenerationError("multipart_storage_unavailable")
        if status.free_bytes <= required_free_bytes(status.total_bytes):
            _terminate(process)
            raise MultipartGenerationError("multipart_storage_insufficient")
        try:
            returncode = process.wait(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            continue
        if returncode != 0:
            raise MultipartGenerationError("ffmpeg_failed")
        return


def _directory_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def validate_generation(
    directory: Path, *, record: Mapping[str, Any], source_probe: MediaProbeResult,
    media_policy: MediaPathPolicy, probe: Callable[[Path], MediaProbeResult] = probe_media,
) -> list[Dict[str, Any]]:
    split = record["split"]
    count = len(split["split_points_seconds"]) + 1
    names = expected_part_names(count, Path(record["media_path"]).suffix)
    files = _directory_files(directory)
    if [path.name for path in files] != list(names):
        raise MultipartGenerationError("multipart_generation_incomplete")
    parts: list[Dict[str, Any]] = []
    total_duration = 0.0
    for index, path in enumerate(files, 1):
        resolved = media_policy.resolve_media_path(path, must_exist=True, require_file=True, allowed_extensions=VIDEO_EXTENSIONS)
        size = resolved.stat().st_size
        if size <= 0:
            raise MultipartGenerationError("multipart_validation_failed")
        try:
            measured = probe(resolved)
        except Exception as exc:
            raise MultipartGenerationError("multipart_validation_failed") from exc
        if not generated_part_within_limits(duration_seconds=measured.duration_seconds, size_bytes=size):
            raise MultipartGenerationError("multipart_replan_required")
        if not stream_signatures_match(source_probe.streams, measured.streams):
            raise MultipartGenerationError("multipart_validation_failed")
        total_duration += measured.duration_seconds
        relative = resolved.relative_to(media_policy.media_root).as_posix()
        parts.append({
            "index": index, "media_path": relative, "size_bytes": size,
            "duration_seconds": measured.duration_seconds,
            "source_kind": "generated", "upload_item_id": None,
            "upload_state": "ready", "attempts": 0,
            "youtube_video_id": None,
            "playlist_state": "pending" if record.get("playlist_id") else "not_requested",
            "reason": None,
        })
    tolerance = max(AGGREGATE_BASE_TOLERANCE_SECONDS, AGGREGATE_PER_PART_TOLERANCE_SECONDS * count)
    if abs(total_duration - float(record["source_duration_seconds"])) > tolerance:
        raise MultipartGenerationError("multipart_validation_failed")
    return parts


class AutoYouTubeGenerationService:
    """Generate and validate one authorized generation; never upload."""
    def __init__(
        self, *, state_store: YouTubeUploadStateStore,
        media_policy: MediaPathPolicy, probe: Callable[[Path], MediaProbeResult] = probe_media,
        popen: Callable[..., Any] = subprocess.Popen,
        storage_assessor: Callable[..., Any] = assess_auto_vod_storage,
        rmtree: Callable[..., Any] = shutil.rmtree,
        replace: Callable[[Path, Path], Any] = lambda source, target: source.replace(target),
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        self._state_store = state_store
        self._media_policy = media_policy
        self._probe = probe
        self._popen = popen
        self._storage_assessor = storage_assessor
        self._rmtree = rmtree
        self._replace = replace
        self._ffmpeg_binary = ffmpeg_binary

    def _attention(self, record: Mapping[str, Any], reason: str) -> str:
        try:
            self._state_store.update_record(
                record["streamer"], record["twitch_vod_id"],
                state="needs_attention", reason=reason,
            )
        except Exception:
            return "pending"
        return "blocked" if reason.startswith("multipart_storage_") else "attention"

    def _validate_existing(self, directory: Path, record: Mapping[str, Any], source_probe: MediaProbeResult) -> Optional[list[Dict[str, Any]]]:
        if not directory.exists():
            return None
        return validate_generation(directory, record=record, source_probe=source_probe, media_policy=self._media_policy, probe=self._probe)

    def generate_record(self, record: Mapping[str, Any]) -> str:
        if record.get("state") == "parts_ready":
            parts = record.get("parts") or []
            if not parts or parts[0].get("source_kind") != "generated":
                return "ready"
            try:
                for part in parts:
                    path = self._media_policy.resolve_media_path(part["media_path"], must_exist=True, require_file=True)
                    if path.stat().st_size != part["size_bytes"]:
                        raise RuntimeError("changed")
                    measured = self._probe(path)
                    if not generated_part_within_limits(duration_seconds=measured.duration_seconds, size_bytes=part["size_bytes"]):
                        raise RuntimeError("invalid")
                    if abs(measured.duration_seconds - part["duration_seconds"]) > 1.0:
                        raise RuntimeError("changed")
                return "ready"
            except Exception:
                return self._attention(record, "multipart_validation_failed")
        recoverable = record.get("state") == "needs_attention" and record.get("reason") in {"multipart_storage_insufficient", "multipart_storage_unavailable"}
        if record.get("state") != "parts_preparing" and not recoverable:
            return "ignored"
        if record.get("reason") == "multipart_replan_required":
            return "ignored"
        try:
            source = validate_completed_auto_youtube_source(record, self._media_policy)
            source_probe = self._probe(source)
            paths = generation_paths(record, self._media_policy)
        except Exception:
            return self._attention(record, "multipart_validation_failed")

        if paths.final.exists():
            try:
                final_parts = self._validate_existing(paths.final, record, source_probe)
                self._state_store.finalize_generated_parts(record["streamer"], record["twitch_vod_id"], parts=final_parts)
                return "ready"
            except MultipartGenerationError as exc:
                return self._attention(record, exc.code)
            except Exception:
                return "pending"
        try:
            staging_parts = self._validate_existing(paths.staging, record, source_probe)
            if staging_parts is not None:
                paths.root.mkdir(parents=True, exist_ok=True)
                self._replace(paths.staging, paths.final)
                final_parts = [{**part, "media_path": part["media_path"].replace("/staging/", "/parts/")} for part in staging_parts]
                self._state_store.finalize_generated_parts(record["streamer"], record["twitch_vod_id"], parts=final_parts)
                return "ready"
        except MultipartGenerationError as exc:
            if exc.code != "multipart_generation_incomplete":
                return self._attention(record, exc.code)
        except Exception:
            return "pending"

        storage = assess_split_storage(self._media_policy.media_root, int(record["size_bytes"]), assessor=self._storage_assessor)
        if not storage.allows_start:
            return self._attention(record, "multipart_storage_unavailable" if storage.state == "unavailable" else "multipart_storage_insufficient")
        try:
            if paths.staging.exists():
                self._rmtree(paths.staging)
            paths.staging.mkdir(parents=True, exist_ok=False)
            command = build_ffmpeg_command(source, paths.staging, record["split"]["split_points_seconds"], len(record["split"]["split_points_seconds"]) + 1, ffmpeg_binary=self._ffmpeg_binary)
            run_ffmpeg_segmentation(command, media_root=self._media_policy.media_root, popen=self._popen, storage_assessor=self._storage_assessor)
            staging_parts = validate_generation(paths.staging, record=record, source_probe=source_probe, media_policy=self._media_policy, probe=self._probe)
            self._replace(paths.staging, paths.final)
            final_parts = [{**part, "media_path": part["media_path"].replace("/staging/", "/parts/")} for part in staging_parts]
            try:
                self._state_store.finalize_generated_parts(record["streamer"], record["twitch_vod_id"], parts=final_parts)
            except Exception:
                return "pending"
            return "ready"
        except MultipartGenerationError as exc:
            return self._attention(record, exc.code)
        except Exception:
            return self._attention(record, "ffmpeg_failed")

    def reconcile(self) -> Dict[str, int]:
        result = {"ready": 0, "blocked": 0, "attention": 0, "pending": 0, "ignored": 0}
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.generate_record(record)
            result[outcome] += 1
        return result
