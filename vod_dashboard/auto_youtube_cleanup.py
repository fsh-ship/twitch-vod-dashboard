"""Read-only cleanup eligibility plus explicit Keep-local mutation validation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


class AutoYouTubeCleanupError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def cleanup_status(
    record: Mapping[str, Any],
    *,
    media_policy: MediaPathPolicy,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Derive presentation/execution eligibility without mutating state or files."""
    cleanup = record["local_cleanup"]
    try:
        source = media_policy.resolve_media_path(
            record["media_path"], must_exist=False
        )
        local_exists = source.is_file() and source.stat().st_size == record["size_bytes"]
    except Exception:
        local_exists = False
    state = "disabled"
    if not local_exists:
        state = "local_copy_missing"
    elif cleanup["policy"] == "manual":
        state = "disabled"
    elif record["state"] != "completed":
        state = "waiting_for_upload"
    elif cleanup["keep_local"]:
        state = "keep_local"
    elif cleanup["cleaned_at"] is not None:
        state = "local_copy_missing"
    else:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise AutoYouTubeCleanupError("invalid_clock")
        state = "due" if _utc(cleanup["cleanup_due_at"]) <= current.astimezone(timezone.utc) else "scheduled"
    return {
        "state": state,
        "policy": cleanup["policy"],
        "delay_hours": cleanup["delay_hours"],
        "cleanup_due_at": cleanup["cleanup_due_at"],
        "keep_local": cleanup["keep_local"],
        "cleaned_at": cleanup["cleaned_at"],
        "can_keep_local": state in {"scheduled", "due"},
        "can_resume_cleanup": state == "keep_local",
    }


class AutoYouTubeCleanupService:
    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        media_policy: MediaPathPolicy,
    ) -> None:
        self._state_store = state_store
        self._media_policy = media_policy

    def set_keep_local(
        self,
        streamer: Any,
        twitch_vod_id: Any,
        *,
        media_path: Any,
        keep_local: bool,
    ) -> Mapping[str, Any]:
        record = self._state_store.get(streamer, twitch_vod_id)
        if record is None:
            raise AutoYouTubeCleanupError("ownership_not_found")
        try:
            requested = self._media_policy.resolve_media_path(
                media_path, must_exist=True, require_file=True
            )
            owned = self._media_policy.resolve_media_path(
                record["media_path"], must_exist=True, require_file=True
            )
            if requested != owned or requested.stat().st_size != record["size_bytes"]:
                raise AutoYouTubeCleanupError("ownership_mismatch")
        except AutoYouTubeCleanupError:
            raise
        except Exception as exc:
            raise AutoYouTubeCleanupError("local_media_invalid") from exc
        try:
            return self._state_store.set_keep_local(
                streamer, twitch_vod_id, keep_local=keep_local
            )
        except Exception as exc:
            code = str(exc)
            if code in {"keep_local_not_allowed", "upload_not_found"}:
                raise AutoYouTubeCleanupError(code) from exc
            raise AutoYouTubeCleanupError("cleanup_persistence_failed") from exc
