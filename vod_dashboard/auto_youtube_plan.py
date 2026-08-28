"""Freeze deterministic Auto YouTube plans without creating upload work."""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from vod_dashboard.auto_vod_result import resolve_completed_auto_vod_output
from vod_dashboard.auto_youtube_multipart import MediaProbeResult, probe_media
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.twitch import canonical_twitch_vod_url
from vod_dashboard.youtube import (
    apply_youtube_template,
    format_duration,
    sanitize_youtube_description,
    sanitize_youtube_title,
)
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStateError,
    YouTubeUploadStateStore,
)


def validate_completed_auto_youtube_source(
    record: Mapping[str, Any], media_policy: MediaPathPolicy
) -> Any:
    """Revalidate exactly the P8c result without discovering a replacement."""
    source_path = media_policy.resolve_media_path(
        record.get("media_path"), must_exist=True, require_file=True
    )
    verified = resolve_completed_auto_vod_output(
        record.get("media_path"), {}, record.get("twitch_vod_id"),
        media_policy=media_policy,
    )
    if (
        verified.get("completed_media_path") != record.get("media_path")
        or verified.get("completed_media_size_bytes") != record.get("size_bytes")
        or verified.get("completed_twitch_vod_id") != record.get("twitch_vod_id")
    ):
        raise RuntimeError("Completed Auto VOD source no longer matches ownership.")
    return source_path


def freeze_plan_inputs(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Capture only deterministic metadata inputs; never secrets or paths."""
    privacy = str(settings.get("youtube_privacy_status") or "private")
    if privacy not in {"private", "unlisted", "public"}:
        privacy = "private"
    tags = [
        tag.strip()
        for tag in str(settings.get("youtube_tags") or "").split(",")
        if tag.strip()
    ]
    return {
        "title_template": str(
            settings.get("youtube_title_template")
            or "{streamer} VOD - {date_de} - {title}"
        ),
        "description_template": str(
            settings.get("youtube_description_template")
            or settings.get("youtube_description")
            or ""
        ),
        "description_fallback": str(settings.get("youtube_description") or ""),
        "privacy_status": privacy,
        "category_id": str(settings.get("youtube_category_id") or "20"),
        "tags": tags,
    }


def _metadata_settings(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "youtube_title_template": inputs["title_template"],
        "youtube_description_template": inputs["description_template"],
        "youtube_description": inputs["description_fallback"],
    }


class AutoYouTubePlanService:
    """Validate exact source media and atomically freeze an upload plan."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        media_policy: MediaPathPolicy,
        metadata_builder: Callable[[Any, Dict[str, Any]], Dict[str, Any]],
        media_probe: Callable[[Any], MediaProbeResult] = probe_media,
    ) -> None:
        self._state_store = state_store
        self._media_policy = media_policy
        self._metadata_builder = metadata_builder
        self._media_probe = media_probe

    @staticmethod
    def _final_metadata(
        metadata: Mapping[str, Any],
        record: Mapping[str, Any],
        inputs: Mapping[str, Any],
        duration_seconds: float,
    ) -> Dict[str, str]:
        """Re-render templates from final structured media/ownership values."""
        raw_meta = metadata.get("meta")
        if not isinstance(raw_meta, Mapping):
            return {
                "title": str(metadata.get("title") or ""),
                "description": str(metadata.get("description") or ""),
            }
        meta = {key: str(value or "") for key, value in raw_meta.items()}
        meta.update(
            {
                "streamer": str(record["streamer"]),
                "vod_id": str(record["twitch_vod_id"]),
                "url": canonical_twitch_vod_url(record["twitch_vod_id"]),
                "duration": format_duration(duration_seconds),
            }
        )
        fallback_title = str(meta.get("title") or metadata.get("title") or "")
        return {
            "title": apply_youtube_template(
                str(inputs["title_template"]), meta, fallback_title
            ),
            "description": apply_youtube_template(
                str(inputs["description_template"]),
                meta,
                str(inputs["description_fallback"]),
            ),
        }

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

    def prepare_record(self, record: Mapping[str, Any]) -> str:
        """Create a plan once, or safely retain a non-retryable attention state."""
        if record.get("state") == "plan_ready" and record.get("upload_plan"):
            return "ready"
        if record.get("state") != "intent_pending":
            return "ignored"
        inputs = record.get("plan_inputs")
        if not isinstance(inputs, Mapping):
            return self._attention(record, "plan_inputs_missing")
        try:
            source_path = validate_completed_auto_youtube_source(
                record, self._media_policy
            )
        except Exception:
            try:
                source_path = self._media_policy.resolve_media_path(
                    record.get("media_path"), must_exist=False
                )
            except Exception:
                source_path = None
            if source_path is not None and not source_path.exists():
                return self._attention(record, "plan_media_missing")
            return self._attention(record, "plan_source_invalid")
        try:
            probe = self._media_probe(source_path)
            metadata = self._metadata_builder(
                self._media_policy.resolve_media_path(
                    record["media_path"], must_exist=True, require_file=True
                ),
                _metadata_settings(inputs),
            )
            final_metadata = self._final_metadata(
                metadata, record, inputs, probe.duration_seconds
            )
            plan = {
                "title": sanitize_youtube_title(final_metadata.get("title")),
                "description": sanitize_youtube_description(
                    final_metadata.get("description")
                ),
                "privacy_status": inputs["privacy_status"],
                "category_id": inputs["category_id"],
                "tags": list(inputs["tags"]),
            }
            self._state_store.set_upload_plan(
                record["streamer"], record["twitch_vod_id"], plan
            )
        except YouTubeUploadStateError:
            return "pending"
        except Exception:
            return self._attention(record, "plan_preparation_failed")
        return "ready"

    def reconcile(self) -> Dict[str, int]:
        """Prepare only pre-existing intents; never create ownership records."""
        result = {"ready": 0, "attention": 0, "pending": 0, "ignored": 0}
        try:
            records = self._state_store.list_records()
        except Exception:
            return result | {"pending": 1}
        for record in records.values():
            outcome = self.prepare_record(record)
            result[outcome] = result.get(outcome, 0) + 1
        return result
