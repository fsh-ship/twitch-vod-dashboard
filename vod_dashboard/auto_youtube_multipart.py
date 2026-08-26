"""Pure analysis and planning primitives for future multipart Auto YouTube work.

This module never writes media or durable state and never talks to YouTube.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import inspect
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Optional, Sequence

from vod_dashboard.youtube import sanitize_youtube_description, sanitize_youtube_title
from vod_dashboard.youtube_upload_state import MAX_DESCRIPTION_LENGTH


HARD_DURATION_SECONDS = 43_200
HARD_SIZE_BYTES = 256_000_000_000
TARGET_DURATION_SECONDS = 42_300
TARGET_SIZE_BYTES = 250_000_000_000
GENERATED_PART_MAX_DURATION_SECONDS = 42_900
PART_PLAN_VERSION = 1
FFPROBE_TIMEOUT_SECONDS = 30
_MILLIS = Decimal("0.001")


class MediaProbeError(RuntimeError):
    """Non-sensitive, stable ffprobe failure."""
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StreamDescriptor:
    codec_type: str
    codec_name: str
    width: Optional[int] = None
    height: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass(frozen=True)
class MediaProbeResult:
    duration_seconds: float
    streams: tuple[StreamDescriptor, ...]
    stream_signature: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class MultipartPlan:
    required: bool
    source_duration_seconds: float
    source_size_bytes: int
    part_count: int
    split_points_seconds: tuple[float, ...]
    target_duration_seconds: int
    target_size_bytes: int
    stream_signature: tuple[tuple[Any, ...], ...]


def _positive_duration(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal("3155760000"):
        return None
    return float(parsed)


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def stream_signature(streams: Sequence[StreamDescriptor]) -> tuple[tuple[Any, ...], ...]:
    """Ordered structural signature; deliberately excludes timing metadata."""
    return tuple(
        (item.codec_type, item.codec_name, item.width, item.height, item.sample_rate, item.channels)
        for item in streams
    )


def stream_signatures_match(source: Sequence[StreamDescriptor], candidate: Sequence[StreamDescriptor]) -> bool:
    return stream_signature(source) == stream_signature(candidate)


def _parse_streams(value: Any) -> tuple[StreamDescriptor, ...]:
    if not isinstance(value, list) or not value:
        raise MediaProbeError("invalid_streams")
    parsed: list[StreamDescriptor] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise MediaProbeError("invalid_streams")
        kind = raw.get("codec_type")
        codec = raw.get("codec_name")
        if kind not in {"video", "audio", "subtitle", "data", "attachment"} or not isinstance(codec, str) or not codec.strip():
            raise MediaProbeError("invalid_streams")
        width = _nonnegative_int(raw.get("width")) if kind == "video" and raw.get("width") is not None else None
        height = _nonnegative_int(raw.get("height")) if kind == "video" and raw.get("height") is not None else None
        sample_rate = _nonnegative_int(raw.get("sample_rate")) if kind == "audio" and raw.get("sample_rate") is not None else None
        channels = _nonnegative_int(raw.get("channels")) if kind == "audio" and raw.get("channels") is not None else None
        if (kind == "video" and ((raw.get("width") is not None and width is None) or (raw.get("height") is not None and height is None))) or (kind == "audio" and ((raw.get("sample_rate") is not None and sample_rate is None) or (raw.get("channels") is not None and channels is None))):
            raise MediaProbeError("invalid_streams")
        parsed.append(StreamDescriptor(kind, codec.strip(), width, height, sample_rate, channels))
    return tuple(parsed)


def parse_ffprobe_payload(payload: Any) -> MediaProbeResult:
    """Parse duration from format first, then a deterministic stream end fallback."""
    if not isinstance(payload, Mapping):
        raise MediaProbeError("invalid_json")
    streams = _parse_streams(payload.get("streams"))
    duration = _positive_duration(payload.get("format", {}).get("duration") if isinstance(payload.get("format"), Mapping) else None)
    if duration is None:
        ends = []
        for raw in payload.get("streams", []):
            start = _positive_duration(raw.get("start_time"))
            stream_duration = _positive_duration(raw.get("duration"))
            if start is not None and stream_duration is not None:
                ends.append(start + stream_duration)
        if not ends:
            raise MediaProbeError("invalid_duration")
        duration = max(ends)
    return MediaProbeResult(duration, streams, stream_signature(streams))


def probe_media(path: Path, *, ffprobe_binary: str = "ffprobe", timeout_seconds: float = FFPROBE_TIMEOUT_SECONDS, runner: Callable[..., Any] = subprocess.run) -> MediaProbeResult:
    """Safely call ffprobe; no stderr or host path is returned to callers."""
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise MediaProbeError("invalid_timeout")
    command = [ffprobe_binary, "-v", "error", "-show_entries", "format=duration", "-show_entries", "stream=index,codec_type,codec_name,start_time,duration,width,height,sample_rate,channels", "-of", "json", str(Path(path))]
    try:
        result = runner(command, shell=False, check=False, capture_output=True, text=True, timeout=timeout_seconds)
    except FileNotFoundError as exc:
        raise MediaProbeError("ffprobe_missing") from exc
    except PermissionError as exc:
        raise MediaProbeError("ffprobe_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError("ffprobe_timeout") from exc
    except OSError as exc:
        raise MediaProbeError("ffprobe_unavailable") from exc
    if getattr(result, "returncode", 1) != 0:
        raise MediaProbeError("ffprobe_failed")
    try:
        return parse_ffprobe_payload(json.loads(getattr(result, "stdout", "")))
    except (TypeError, ValueError) as exc:
        raise MediaProbeError("invalid_json") from exc


def _ceil_division(value: int | float, divisor: int) -> int:
    return int(math.ceil(value / divisor))


def plan_multipart_upload(*, duration_seconds: float, size_bytes: int, signature: Sequence[StreamDescriptor] = ()) -> MultipartPlan:
    if _positive_duration(duration_seconds) is None or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ValueError("invalid_source_measurement")
    duration = float(duration_seconds)
    required = duration >= HARD_DURATION_SECONDS or size_bytes >= HARD_SIZE_BYTES
    count = 1 if not required else max(2, _ceil_division(duration, TARGET_DURATION_SECONDS), _ceil_division(size_bytes, TARGET_SIZE_BYTES))
    points: tuple[float, ...] = ()
    if count > 1:
        source = Decimal(str(duration))
        points = tuple(float((source * Decimal(index) / Decimal(count)).quantize(_MILLIS, rounding=ROUND_HALF_UP)) for index in range(1, count))
        if not all(0 < point < duration for point in points) or any(left >= right for left, right in zip(points, points[1:])):
            raise ValueError("invalid_split_points")
    descriptors = tuple(signature)
    return MultipartPlan(required, duration, size_bytes, count, points, TARGET_DURATION_SECONDS, TARGET_SIZE_BYTES, stream_signature(descriptors))


def original_part_within_limits(*, duration_seconds: float, size_bytes: int) -> bool:
    return _positive_duration(duration_seconds) is not None and duration_seconds < HARD_DURATION_SECONDS and isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and 0 < size_bytes < HARD_SIZE_BYTES


def generated_part_within_limits(*, duration_seconds: float, size_bytes: int) -> bool:
    return _positive_duration(duration_seconds) is not None and duration_seconds <= GENERATED_PART_MAX_DURATION_SECONDS and duration_seconds < HARD_DURATION_SECONDS and isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and 0 < size_bytes <= TARGET_SIZE_BYTES and size_bytes < HARD_SIZE_BYTES


def _title_limit() -> int:
    return int(inspect.signature(sanitize_youtube_title).parameters["max_len"].default)


def derive_part_upload_plan(base_plan: Mapping[str, Any], *, index: int, total: int) -> dict[str, Any]:
    """Derive immutable per-part metadata solely from the frozen P8e plan."""
    if not isinstance(index, int) or not isinstance(total, int) or index < 1 or total < index:
        raise ValueError("invalid_part_index")
    base_title = str(base_plan.get("title") or "")
    base_description = str(base_plan.get("description") or "")
    if total == 1:
        return {**dict(base_plan), "title": sanitize_youtube_title(base_title), "description": sanitize_youtube_description(base_description)}
    suffix = f" (Part {index}/{total})"
    maximum = _title_limit()
    safe_base = sanitize_youtube_title(base_title, max_len=max(1, maximum - len(suffix)))
    title = sanitize_youtube_title(safe_base + suffix, max_len=maximum)
    prefix = f"Part {index} of {total}.\n\n"
    available = max(0, MAX_DESCRIPTION_LENGTH - len(prefix))
    description = sanitize_youtube_description(prefix + sanitize_youtube_description(base_description)[:available])
    return {**dict(base_plan), "title": title, "description": description}
