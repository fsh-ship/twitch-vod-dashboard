"""Conservative per-part execution for explicitly released Auto YouTube jobs."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from vod_dashboard.auto_youtube_materialize import (
    AutoYouTubeMaterializationService,
    _InvalidMaterializationMedia,
    _MissingMaterializationMedia,
)
from vod_dashboard.auto_youtube_multipart import MediaProbeResult, derive_part_upload_plan, probe_media
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube import YouTubeNotConnectedError
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


class AutoYouTubeExecutionError(RuntimeError):
    """Stable internal refusal; messages never include credentials or paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AutoYouTubeExecutionService:
    """Own the ledger-first boundary around resumable video transmission."""

    def __init__(
        self,
        *,
        state_store: YouTubeUploadStateStore,
        job_manager: Any,
        media_policy: MediaPathPolicy,
        settings_provider: Callable[[], Mapping[str, Any]],
        service_getter: Callable[..., Any],
        request_builder: Callable[[Any, Path, Mapping[str, Any], Mapping[str, Any]], Any],
        request_sender: Callable[..., Optional[str]],
        probe: Callable[[Path], MediaProbeResult] = probe_media,
        log: Optional[Callable[[str, str], None]] = None,
        playlist_chainer: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._state_store = state_store
        self._job_manager = job_manager
        self._media_policy = media_policy
        self._settings_provider = settings_provider
        self._service_getter = service_getter
        self._request_builder = request_builder
        self._request_sender = request_sender
        self._probe = probe
        self._log = log or (lambda _job_id, _message: None)
        self._playlist_chainer = playlist_chainer
        self._automatic_worker_starts: set[str] = set()

    def _materializer(self) -> AutoYouTubeMaterializationService:
        return AutoYouTubeMaterializationService(
            state_store=self._state_store,
            job_manager=self._job_manager,
            media_policy=self._media_policy,
            probe=self._probe,
        )

    @staticmethod
    def _body(plan: Mapping[str, Any], *, index: int, total: int) -> Dict[str, Any]:
        derived = derive_part_upload_plan(plan, index=index, total=total)
        return {
            "snippet": {
                "title": derived["title"],
                "description": derived["description"],
                "tags": list(derived["tags"]),
                "categoryId": derived["category_id"],
            },
            "status": {
                "privacyStatus": derived["privacy_status"],
                "selfDeclaredMadeForKids": False,
            },
        }

    def _ownership(self, job_id: str) -> tuple[Mapping[str, Any], Mapping[str, Any], list[Dict[str, Any]]]:
        job = self._job_manager.get_job(str(job_id))
        if not isinstance(job, Mapping) or job.get("type") != "youtube_upload" or job.get("origin") != "auto_youtube":
            raise AutoYouTubeExecutionError("invalid_auto_youtube_job")
        context = job.get("auto_youtube_context")
        if not isinstance(context, Mapping):
            raise AutoYouTubeExecutionError("invalid_auto_youtube_job")
        record = self._state_store.get(context.get("streamer"), context.get("twitch_vod_id"))
        if not isinstance(record, Mapping) or str(record.get("upload_job_id") or "") != str(job_id):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        materializer = self._materializer()
        try:
            descriptors = materializer._part_descriptors(record)
            metadata = materializer._metadata(record, record.get("upload_plan"), descriptors)
        except Exception as exc:
            raise AutoYouTubeExecutionError("ownership_mismatch") from exc
        if not materializer._matches(
            job, record, str(job.get("auto_youtube_key") or ""), descriptors, metadata
        ):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        return job, record, descriptors

    def _validate_release_candidate(
        self, job_id: str, *, deferred: Optional[bool]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubeExecutionError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubeExecutionError("job_store_unavailable")
        job, record, descriptors = self._ownership(str(job_id))
        if (
            (deferred is not None and job.get("execution_deferred") is not deferred)
            or record.get("state") != "upload_queued"
        ):
            raise AutoYouTubeExecutionError("release_not_allowed")
        lineage = [
            candidate for candidate in self._job_manager.snapshot_jobs()
            if candidate.get("origin") == "auto_youtube"
            and candidate.get("auto_youtube_key") == job.get("auto_youtube_key")
        ]
        if len(lineage) != 1 or str(lineage[0].get("id") or "") != str(job_id):
            raise AutoYouTubeExecutionError("conflicting_ownership")
        item_ids = list(job.get("item_ids") or [])
        parts = list(record.get("parts") or [])
        if (
            len(item_ids) != len(parts)
            or any(part.get("upload_item_id") != item_id for part, item_id in zip(parts, item_ids))
            or any(part.get("upload_state") != "queued" or part.get("youtube_video_id") is not None for part in parts)
        ):
            raise AutoYouTubeExecutionError("release_not_allowed")
        try:
            self._materializer()._validate_media(record, descriptors)
        except (_MissingMaterializationMedia, _InvalidMaterializationMedia) as exc:
            raise AutoYouTubeExecutionError("release_media_invalid") from exc
        return job, record

    def release_auto_youtube_job_for_execution(self, job_id: str) -> bool:
        """Validate ownership, then durably clear the existing execution gate."""
        self._validate_release_candidate(str(job_id), deferred=True)
        if not self._job_manager.release_auto_youtube_job_for_execution(str(job_id)):
            raise AutoYouTubeExecutionError("release_not_allowed")
        return True

    def release_automatic_jobs_for_execution(
        self,
        worker_starter: Callable[[str], Any],
        *,
        recover_released: bool = False,
    ) -> Dict[str, int]:
        """Release only owners with frozen automatic policy, then arm workers."""
        result = {
            "released": 0,
            "recovered": 0,
            "already_started": 0,
            "pending": 0,
            "ignored": 0,
        }
        try:
            records = self._state_store.list_records()
        except Exception:
            result["pending"] += 1
            return result
        for record in records.values():
            if (
                record.get("execution_policy") != "automatic"
                or record.get("state") != "upload_queued"
                or not record.get("upload_job_id")
            ):
                result["ignored"] += 1
                continue
            job_id = str(record["upload_job_id"])
            if job_id in self._automatic_worker_starts:
                result["already_started"] += 1
                continue
            released_now = False
            try:
                job = self._job_manager.get_job(job_id) or {}
                if job.get("execution_deferred") is True:
                    self.release_auto_youtube_job_for_execution(job_id)
                    released_now = True
                elif recover_released:
                    self._validate_release_candidate(job_id, deferred=False)
                    if "queued" not in list(job.get("item_states") or []):
                        result["ignored"] += 1
                        continue
                else:
                    result["ignored"] += 1
                    continue
                self._automatic_worker_starts.add(job_id)
                worker_starter(job_id)
            except Exception:
                self._automatic_worker_starts.discard(job_id)
                if released_now or recover_released:
                    try:
                        self._job_manager.defer_auto_youtube_job(job_id)
                    except Exception:
                        pass
                result["pending"] += 1
                continue
            result["released" if released_now else "recovered"] += 1
        return result

    def _block(
        self,
        job: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        item_id: str,
        index: int,
        reason: str,
        uncertain: bool,
    ) -> None:
        try:
            self._state_store.mark_part_attention(
                record["streamer"], record["twitch_vod_id"],
                upload_job_id=str(job["id"]), upload_item_id=item_id,
                part_index=index + 1, reason=reason, uncertain=uncertain,
            )
        except Exception:
            pass
        try:
            self._job_manager.block_auto_youtube_item(
                str(job["id"]), item_id, uncertain=uncertain, reason=reason
            )
        except Exception:
            # The in-memory method applies the safer gate before its required
            # save. The ledger remains the restart authority if JobStore fails.
            pass

    def _execute_claimed(self, job_id: str, claimed: Mapping[str, Any]) -> bool:
        item_id = str(claimed.get("item_id") or "")
        index = int(claimed.get("index"))
        try:
            job, record, descriptors = self._ownership(job_id)
            if job.get("execution_deferred") is not False or record.get("state") != "upload_queued":
                raise AutoYouTubeExecutionError("execution_not_released")
            part = record["parts"][index]
            if part.get("upload_item_id") != item_id or part.get("upload_state") != "queued":
                raise AutoYouTubeExecutionError("ownership_mismatch")
            self._materializer()._validate_media(record, descriptors)
            path = self._media_policy.resolve_media_path(
                part["media_path"], must_exist=True, require_file=True
            )
        except _MissingMaterializationMedia:
            job = self._job_manager.get_job(job_id) or {"id": job_id}
            record = locals().get("record") or {}
            self._block(job, record, item_id=item_id, index=index, reason="materialization_media_missing", uncertain=False)
            return False
        except Exception:
            job = self._job_manager.get_job(job_id) or {"id": job_id}
            record = locals().get("record") or {}
            self._block(job, record, item_id=item_id, index=index, reason="materialization_source_invalid", uncertain=False)
            return False

        try:
            settings = dict(self._settings_provider())
            body = self._body(
                record["upload_plan"], index=index + 1,
                total=len(descriptors),
            )
            service = self._service_getter(settings, interactive=False)
            request = self._request_builder(service, path, body, settings)
        except YouTubeNotConnectedError:
            self._block(job, record, item_id=item_id, index=index, reason="youtube_not_connected", uncertain=False)
            return False
        except Exception:
            self._block(job, record, item_id=item_id, index=index, reason="api_unavailable", uncertain=False)
            return False

        try:
            self._state_store.begin_part_transfer(
                record["streamer"], record["twitch_vod_id"],
                upload_job_id=job_id, upload_item_id=item_id,
                part_index=index + 1,
            )
        except Exception:
            try:
                self._job_manager.defer_auto_youtube_job(job_id)
            except Exception:
                pass
            return False

        try:
            video_id = self._request_sender(
                request,
                progress_callback=lambda uploaded, total: self._job_manager.update_active_upload_progress(
                    job_id, uploaded, total, item_id=item_id
                ),
                fallback_total_bytes=part["size_bytes"],
            )
            if not video_id:
                raise AutoYouTubeExecutionError("missing_video_id")
        except Exception:
            self._block(job, record, item_id=item_id, index=index, reason="upload_outcome_uncertain", uncertain=True)
            return False

        try:
            confirmed_record = self._state_store.confirm_part_video(
                record["streamer"], record["twitch_vod_id"],
                upload_job_id=job_id, upload_item_id=item_id,
                part_index=index + 1, youtube_video_id=video_id,
            )
        except Exception:
            self._block(job, record, item_id=item_id, index=index, reason="upload_outcome_uncertain", uncertain=True)
            return False
        try:
            if not self._job_manager.complete_auto_youtube_item(job_id, item_id):
                raise AutoYouTubeExecutionError("job_completion_failed")
        except Exception:
            try:
                self._job_manager.defer_auto_youtube_job(job_id)
            except Exception:
                pass
            return False
        self._log(job_id, f"Auto YouTube video part {index + 1}/{len(descriptors)} confirmed.")
        completed_job = self._job_manager.get_job(job_id) or {}
        if (
            self._playlist_chainer is not None
            and confirmed_record.get("state") == "playlist_pending"
            and list(completed_job.get("item_states") or [])
            == ["completed"] * len(descriptors)
        ):
            try:
                self._playlist_chainer(job_id)
            except Exception:
                # Video ownership is already durable. Playlist handling is a
                # separate post-upload action and must never roll the upload
                # back, requeue it, or cause a second video transmission.
                self._log(
                    job_id,
                    "Automatic YouTube playlist processing could not be completed. "
                    "Review the playlist status before another action.",
                )
        return True

    def run_job(self, job_id: str) -> None:
        """Execute only an already-released Auto YouTube job, in item order."""
        job = self._job_manager.get_job(str(job_id)) or {}
        if job.get("origin") != "auto_youtube" or job.get("execution_deferred") is not False:
            return
        while True:
            claimed = self._job_manager.claim_next_item(str(job_id))
            if claimed is None or not self._execute_claimed(str(job_id), claimed):
                return

    def reconcile(self) -> Dict[str, int]:
        """Repair restart states from ledger authority without releasing work."""
        result = {"deferred": 0, "queued": 0, "confirmed": 0, "blocked": 0, "pending": 0}
        for snapshot in self._job_manager.snapshot_jobs():
            if snapshot.get("origin") != "auto_youtube":
                continue
            job_id = str(snapshot.get("id") or "")
            try:
                job, record, _descriptors = self._ownership(job_id)
            except Exception:
                try:
                    self._job_manager.defer_auto_youtube_job(job_id)
                except Exception:
                    result["pending"] += 1
                else:
                    result["blocked"] += 1
                continue
            states = list(job.get("item_states") or [])
            blocked = record.get("state") == "needs_attention"
            for index, (part, item_id) in enumerate(zip(record.get("parts") or [], job.get("item_ids") or [])):
                ledger_state = part.get("upload_state")
                job_state = states[index]
                if ledger_state == "transfer_started" and part.get("youtube_video_id") is None:
                    self._block(job, record, item_id=item_id, index=index, reason="upload_outcome_uncertain", uncertain=True)
                    blocked = True
                    result["blocked"] += 1
                    break
                if ledger_state in {"uncertain", "failed_known"}:
                    try:
                        self._job_manager.block_auto_youtube_item(
                            job_id, item_id,
                            uncertain=ledger_state == "uncertain",
                            reason=str(part.get("reason") or "materialization_consistency_error"),
                        )
                    except Exception:
                        result["pending"] += 1
                    blocked = True
                    result["blocked"] += 1
                    break
                if ledger_state in {"video_confirmed", "completed"} and part.get("youtube_video_id"):
                    if job_state != "completed":
                        try:
                            self._job_manager.complete_auto_youtube_item(job_id, item_id)
                        except Exception:
                            result["pending"] += 1
                            return result
                    result["confirmed"] += 1
                    continue
                if ledger_state == "queued" and job_state == "completed":
                    self._block(job, record, item_id=item_id, index=index, reason="materialization_consistency_error", uncertain=False)
                    blocked = True
                    result["blocked"] += 1
                    break
                if ledger_state == "queued" and job_state != "queued":
                    try:
                        self._job_manager.reset_auto_youtube_item_to_queued(job_id, item_id)
                    except Exception:
                        result["pending"] += 1
                        return result
            if blocked:
                try:
                    self._job_manager.defer_auto_youtube_job(job_id)
                except Exception:
                    result["pending"] += 1
            else:
                current = self._job_manager.get_job(job_id) or job
                parts = list(record.get("parts") or [])
                item_ids = list(current.get("item_ids") or [])
                all_confirmed = bool(parts) and len(parts) == len(item_ids) and all(
                    part.get("upload_state") in {"video_confirmed", "completed"}
                    and bool(part.get("youtube_video_id"))
                    for part in parts
                )
                if (
                    all_confirmed
                    and current.get("execution_deferred") is True
                    and list(current.get("item_states") or [])
                    == ["completed"] * len(parts)
                ):
                    try:
                        # No item transition is needed here, but reuse the
                        # required completion save to converge a stale durable
                        # deferred gate after a prior confirmed completion.
                        self._job_manager.complete_auto_youtube_item(
                            job_id, item_ids[0]
                        )
                        current = self._job_manager.get_job(job_id) or current
                    except Exception:
                        result["pending"] += 1
                        return result
                if current.get("execution_deferred") is True:
                    result["deferred"] += 1
                else:
                    result["queued"] += 1
        return result
