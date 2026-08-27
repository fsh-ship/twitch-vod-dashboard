"""Explicit, ledger-owned YouTube playlist insertion for Auto YouTube."""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from vod_dashboard.auto_youtube_materialize import AutoYouTubeMaterializationService
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStatePersistenceError,
    YouTubeUploadStateStore,
)


class AutoYouTubePlaylistError(RuntimeError):
    """Stable refusal for the explicit playlist action."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def playlist_contains_video(
    service: Any, playlist_id: str, youtube_video_id: str
) -> bool:
    """Read every playlist page and compare the exact remote video ID."""
    page_token: Optional[str] = None
    while True:
        request_args: Dict[str, Any] = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            request_args["pageToken"] = page_token
        response = service.playlistItems().list(**request_args).execute()
        if not isinstance(response, Mapping):
            raise AutoYouTubePlaylistError("playlist_lookup_failed")
        for item in response.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            resource = item.get("snippet", {}).get("resourceId", {})
            if isinstance(resource, Mapping) and resource.get("videoId") == youtube_video_id:
                return True
        page_token = response.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return False


def insert_video_into_playlist(
    service: Any, playlist_id: str, youtube_video_id: str
) -> Mapping[str, Any]:
    """Perform exactly one explicit playlist mutation after durable intent."""
    response = service.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": youtube_video_id,
                },
            }
        },
    ).execute()
    if not isinstance(response, Mapping) or not isinstance(response.get("id"), str) or not response["id"].strip():
        raise AutoYouTubePlaylistError("playlist_insert_uncertain")
    return response


class AutoYouTubePlaylistService:
    """Own explicit, serial playlist membership confirmation from the ledger."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        job_manager: Any,
        media_policy: MediaPathPolicy,
        settings_provider: Callable[[], Mapping[str, Any]],
        service_getter: Callable[..., Any],
        membership_lookup: Callable[[Any, str, str], bool] = playlist_contains_video,
        playlist_inserter: Callable[[Any, str, str], Mapping[str, Any]] = insert_video_into_playlist,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._state_store = state_store
        self._job_manager = job_manager
        self._media_policy = media_policy
        self._settings_provider = settings_provider
        self._service_getter = service_getter
        self._membership_lookup = membership_lookup
        self._playlist_inserter = playlist_inserter
        self._log = log or (lambda _job_id, _message: None)

    def _ownership(self, job_id: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        job = self._job_manager.get_job(str(job_id))
        if (
            not isinstance(job, Mapping)
            or job.get("type") != "youtube_upload"
            or job.get("origin") != "auto_youtube"
            or job.get("execution_deferred") is not False
        ):
            raise AutoYouTubePlaylistError("invalid_auto_youtube_job")
        context = job.get("auto_youtube_context")
        if not isinstance(context, Mapping):
            raise AutoYouTubePlaylistError("ownership_mismatch")
        record = self._state_store.get(
            context.get("streamer"), context.get("twitch_vod_id")
        )
        if (
            not isinstance(record, Mapping)
            or str(record.get("upload_job_id") or "") != str(job_id)
        ):
            raise AutoYouTubePlaylistError("ownership_mismatch")
        materializer = AutoYouTubeMaterializationService(
            state_store=self._state_store,
            job_manager=self._job_manager,
            media_policy=self._media_policy,
        )
        try:
            descriptors = materializer._part_descriptors(record)
            metadata = materializer._metadata(
                record, record.get("upload_plan"), descriptors
            )
        except Exception as exc:
            raise AutoYouTubePlaylistError("ownership_mismatch") from exc
        if not materializer._matches(
            job,
            record,
            str(job.get("auto_youtube_key") or ""),
            descriptors,
            metadata,
        ):
            raise AutoYouTubePlaylistError("ownership_mismatch")
        lineage = [
            candidate
            for candidate in self._job_manager.snapshot_jobs()
            if candidate.get("origin") == "auto_youtube"
            and candidate.get("auto_youtube_key") == job.get("auto_youtube_key")
        ]
        if len(lineage) != 1 or str(lineage[0].get("id") or "") != str(job_id):
            raise AutoYouTubePlaylistError("conflicting_ownership")
        return job, record

    @staticmethod
    def _all_video_confirmed(record: Mapping[str, Any]) -> bool:
        parts = record.get("parts") or []
        return bool(parts) and all(
            part.get("upload_state") in {"video_confirmed", "completed"}
            and isinstance(part.get("youtube_video_id"), str)
            and bool(part.get("youtube_video_id"))
            for part in parts
        )

    def _eligible(self, job: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
        return (
            record.get("state") == "playlist_pending"
            and bool(record.get("playlist_id"))
            and self._all_video_confirmed(record)
            and list(job.get("item_states") or []) == [
                "completed"
            ] * len(record.get("parts") or [])
            and any(
                part.get("playlist_state") == "pending"
                for part in record.get("parts") or []
            )
            and all(
                part.get("playlist_state") in {"pending", "confirmed"}
                for part in record.get("parts") or []
            )
        )

    def status_for_jobs(
        self, jobs: list[Mapping[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Return read-only, non-sensitive UI state for known auto jobs."""
        result: Dict[str, Dict[str, Any]] = {}
        for job in jobs:
            if (
                job.get("type") != "youtube_upload"
                or job.get("origin") != "auto_youtube"
            ):
                continue
            context = job.get("auto_youtube_context")
            if not isinstance(context, Mapping):
                continue
            record = self._state_store.get(
                context.get("streamer"), context.get("twitch_vod_id")
            )
            if (
                not isinstance(record, Mapping)
                or str(record.get("upload_job_id") or "") != str(job.get("id") or "")
            ):
                continue
            parts = list(record.get("parts") or [])
            result[str(job.get("id"))] = {
                "state": str(record.get("state") or ""),
                "eligible": self._eligible(job, record),
                "pending_parts": sum(
                    part.get("playlist_state") == "pending" for part in parts
                ),
                "part_count": len(parts),
            }
        return result

    def add_to_playlist(self, job_id: str) -> Dict[str, Any]:
        """Perform one administrator-requested, serial playlist handoff."""
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubePlaylistError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubePlaylistError("job_store_unavailable")
        job, record = self._ownership(str(job_id))
        if record.get("state") == "completed":
            return {"status": "already_confirmed", "completed": True}
        if record.get("state") == "needs_attention":
            raise AutoYouTubePlaylistError("needs_attention")
        if not self._eligible(job, record):
            raise AutoYouTubePlaylistError("playlist_not_pending")

        try:
            service = self._service_getter(
                dict(self._settings_provider()), interactive=False
            )
        except Exception as exc:
            raise AutoYouTubePlaylistError("playlist_lookup_failed") from exc

        playlist_id = str(record["playlist_id"])
        for index, part in enumerate(record["parts"], 1):
            item_id = str(part["upload_item_id"])
            video_id = str(part["youtube_video_id"])
            if part.get("playlist_state") == "confirmed":
                continue
            try:
                present = self._membership_lookup(service, playlist_id, video_id)
            except Exception as exc:
                raise AutoYouTubePlaylistError("playlist_lookup_failed") from exc
            if present:
                try:
                    record = self._state_store.confirm_part_playlist_membership(
                        record["streamer"], record["twitch_vod_id"],
                        upload_job_id=job_id, upload_item_id=item_id,
                        part_index=index,
                    )
                except Exception as exc:
                    raise AutoYouTubePlaylistError("playlist_persistence_failed") from exc
                continue
            try:
                record = self._state_store.begin_part_playlist_insertion(
                    record["streamer"], record["twitch_vod_id"],
                    upload_job_id=job_id, upload_item_id=item_id,
                    part_index=index,
                )
            except Exception as exc:
                raise AutoYouTubePlaylistError("playlist_persistence_failed") from exc
            try:
                self._playlist_inserter(service, playlist_id, video_id)
            except Exception:
                try:
                    present_after_error = self._membership_lookup(
                        service, playlist_id, video_id
                    )
                except Exception:
                    present_after_error = False
                if present_after_error:
                    try:
                        record = self._state_store.confirm_part_playlist_membership(
                            record["streamer"], record["twitch_vod_id"],
                            upload_job_id=job_id, upload_item_id=item_id,
                            part_index=index,
                        )
                    except Exception as exc:
                        raise AutoYouTubePlaylistError("playlist_persistence_failed") from exc
                    continue
                try:
                    self._state_store.mark_part_playlist_attention(
                        record["streamer"], record["twitch_vod_id"],
                        upload_job_id=job_id, upload_item_id=item_id,
                        part_index=index, reason="playlist_uncertain",
                    )
                except Exception as exc:
                    raise AutoYouTubePlaylistError("playlist_persistence_failed") from exc
                raise AutoYouTubePlaylistError("needs_attention")
            try:
                record = self._state_store.confirm_part_playlist_membership(
                    record["streamer"], record["twitch_vod_id"],
                    upload_job_id=job_id, upload_item_id=item_id,
                    part_index=index,
                )
            except Exception as exc:
                raise AutoYouTubePlaylistError("playlist_persistence_failed") from exc
            self._log(job_id, f"Auto YouTube playlist part {index}/{len(record['parts'])} confirmed.")

        return {
            "status": "completed" if record.get("state") == "completed" else "playlist_pending",
            "completed": record.get("state") == "completed",
        }
