"""Read-only Auto YouTube ownership checks for local manual-upload admission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from vod_dashboard.auto_vod import normalize_auto_vod_id
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.settings import canonical_streamer_login


OWNED_BUNDLE_STATES = frozenset(
    {
        "intent_pending",
        "plan_ready",
        "parts_preparing",
        "parts_ready",
        "upload_queued",
        "video_confirmed",
        "playlist_pending",
        "completed",
        "blocked_youtube",
        "needs_attention",
    }
)
CONFIRMED_BUNDLE_STATES = frozenset(
    {"video_confirmed", "playlist_pending", "completed"}
)


class AutoYouTubeOwnershipUnavailable(RuntimeError):
    """Ownership could not be checked safely."""

    def __init__(self) -> None:
        super().__init__("Auto YouTube ownership could not be verified.")


class AutoYouTubeOwnedMediaError(RuntimeError):
    """The exact local media is already owned by Auto YouTube."""

    def __init__(self) -> None:
        super().__init__("This VOD is managed by Auto YouTube.")


@dataclass(frozen=True)
class AutoYouTubeOwnership:
    managed: bool
    video_confirmed: bool

    @property
    def status(self) -> str:
        if self.video_confirmed:
            return "Uploaded by Auto YouTube"
        if self.managed:
            return "Managed by Auto YouTube"
        return ""


def load_ownership_records(state_store: Any) -> tuple[Mapping[str, Any], ...]:
    """Load one validated ledger snapshot or fail closed."""
    try:
        records = state_store.list_records()
    except Exception as exc:
        raise AutoYouTubeOwnershipUnavailable() from exc
    if not isinstance(records, Mapping):
        raise AutoYouTubeOwnershipUnavailable()
    return tuple(
        record for record in records.values() if isinstance(record, Mapping)
    )


def ownership_for_local_media(
    path: Path,
    *,
    streamer: Any,
    twitch_vod_id: Any,
    records: Iterable[Mapping[str, Any]],
    media_policy: MediaPathPolicy,
) -> AutoYouTubeOwnership:
    """Match canonical VOD ownership plus the exact ledger media path."""
    record = record_for_local_media(
        path,
        streamer=streamer,
        twitch_vod_id=twitch_vod_id,
        records=records,
        media_policy=media_policy,
    )
    if record is None:
        return AutoYouTubeOwnership(False, False)
    parts = record.get("parts")
    part_confirmed = isinstance(parts, list) and any(
        isinstance(part, Mapping)
        and bool(str(part.get("youtube_video_id") or "").strip())
        and str(part.get("upload_state") or "")
        in {"video_confirmed", "completed"}
        for part in parts
    )
    confirmed = (
        str(record.get("state") or "") in CONFIRMED_BUNDLE_STATES
        or part_confirmed
    )
    return AutoYouTubeOwnership(True, confirmed)


def record_for_local_media(
    path: Path,
    *,
    streamer: Any,
    twitch_vod_id: Any,
    records: Iterable[Mapping[str, Any]],
    media_policy: MediaPathPolicy,
) -> Mapping[str, Any] | None:
    """Return the validated owner for one exact canonical local source."""
    vod_id = normalize_auto_vod_id(twitch_vod_id)
    canonical_streamer = canonical_streamer_login(streamer)
    try:
        local_path = media_policy.resolve_media_path(
            path, must_exist=True, require_file=True
        )
        local_size = local_path.stat().st_size
    except Exception:
        return None

    for record in records:
        if str(record.get("state") or "") not in OWNED_BUNDLE_STATES:
            continue
        try:
            owned_path = media_policy.resolve_media_path(
                record.get("media_path"), must_exist=False
            )
        except Exception:
            continue
        if (
            owned_path != local_path
            or isinstance(record.get("size_bytes"), bool)
            or record.get("size_bytes") != local_size
        ):
            continue
        if vod_id and str(record.get("twitch_vod_id") or "") != vod_id:
            continue
        record_streamer = canonical_streamer_login(record.get("streamer"))
        if vod_id and canonical_streamer and record_streamer != canonical_streamer:
            continue
        return record
    return None


def require_manual_upload_eligible(
    path: Path,
    *,
    streamer: Any,
    twitch_vod_id: Any,
    records: Iterable[Mapping[str, Any]],
    media_policy: MediaPathPolicy,
) -> None:
    ownership = ownership_for_local_media(
        path,
        streamer=streamer,
        twitch_vod_id=twitch_vod_id,
        records=records,
        media_policy=media_policy,
    )
    if ownership.managed:
        raise AutoYouTubeOwnedMediaError()
