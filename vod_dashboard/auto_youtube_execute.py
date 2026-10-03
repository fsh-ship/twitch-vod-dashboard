"""Conservative per-part execution for explicitly released Auto YouTube jobs."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from vod_dashboard.auto_youtube_materialize import (
    AutoYouTubeMaterializationService,
    _InvalidMaterializationMedia,
    _MissingMaterializationMedia,
)
from vod_dashboard.auto_youtube_multipart import MediaProbeResult, derive_part_upload_plan, probe_media
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube import (
    YouTubeNotConnectedError,
    youtube_video_belongs_to_connected_channel,
)
from vod_dashboard.youtube_upload_state import (
    YouTubeUploadStatePersistenceError,
    YouTubeUploadStateStore,
    YouTubeUploadStateValidationError,
    SAFE_KNOWN_PRETRANSFER_RECOVERY_REASONS,
    canonical_upload_key,
)


class AutoYouTubeExecutionError(RuntimeError):
    """Stable internal refusal; messages never include credentials or paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def youtube_video_id_from_input(value: Any) -> Optional[str]:
    """Accept a video ID or a supported YouTube URL, never an arbitrary host."""
    raw = value.strip() if isinstance(value, str) else ""
    video_id = raw
    if raw.startswith("https://"):
        try:
            parsed = urlsplit(raw)
            if parsed.username or parsed.password or parsed.port or parsed.fragment:
                return None
            host = (parsed.hostname or "").lower()
            if host == "youtu.be" and parsed.path.count("/") == 1:
                video_id = parsed.path[1:]
            elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                if parsed.path == "/watch":
                    values = parse_qs(parsed.query).get("v", [])
                    if len(values) != 1:
                        return None
                    video_id = values[0]
                elif parsed.path.startswith(("/shorts/", "/live/")) and parsed.path.count("/") == 2:
                    video_id = parsed.path.rsplit("/", 1)[1]
                else:
                    return None
            else:
                return None
        except ValueError:
            return None
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else None


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

    def _ownership(
        self,
        job_id: str,
        *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], list[Dict[str, Any]]]:
        job = self._job_manager.get_job(str(job_id))
        if not isinstance(job, Mapping) or job.get("type") != "youtube_upload" or job.get("origin") != "auto_youtube":
            raise AutoYouTubeExecutionError("invalid_auto_youtube_job")
        context = job.get("auto_youtube_context")
        if not isinstance(context, Mapping):
            raise AutoYouTubeExecutionError("invalid_auto_youtube_job")
        if records is None:
            record = self._state_store.get(
                context.get("streamer"), context.get("twitch_vod_id")
            )
        else:
            try:
                record = records.get(
                    canonical_upload_key(
                        context.get("streamer"), context.get("twitch_vod_id")
                    )
                )
            except YouTubeUploadStateValidationError:
                record = None
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

    def _continuation_candidate(
        self,
        job_id: str,
        *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], list[Dict[str, Any]], int]:
        """Validate the exact confirmed-prefix/queued-suffix continuation state."""
        job, record, descriptors = self._ownership(job_id, records=records)
        self._validate_unique_lineage(job_id, job)
        item_ids = list(job.get("item_ids") or [])
        states = list(job.get("item_states") or [])
        failure_kinds = list(job.get("item_failure_kinds") or [])
        completion_reasons = list(job.get("item_completion_reasons") or [])
        recovery_reasons = list(job.get("item_recovery_reasons") or [])
        retry_ids = list(job.get("item_retry_job_ids") or [])
        parts = list(record.get("parts") or [])
        if (
            job.get("execution_deferred") is not True
            or job.get("state") != "queued"
            or record.get("state") != "upload_queued"
            or not (
                len(parts) == len(item_ids) == len(states) == len(failure_kinds)
                == len(completion_reasons) == len(recovery_reasons) == len(retry_ids)
            )
        ):
            raise AutoYouTubeExecutionError("continuation_not_allowed")
        first_queued = None
        for index, (part, item_id) in enumerate(zip(parts, item_ids)):
            if part.get("upload_item_id") != item_id:
                raise AutoYouTubeExecutionError("ownership_mismatch")
            confirmed = (
                part.get("upload_state") in {"video_confirmed", "completed"}
                and bool(part.get("youtube_video_id"))
                and states[index] == "completed"
                and failure_kinds[index] == ""
                and recovery_reasons[index] == ""
                and not retry_ids[index]
            )
            queued = (
                part.get("upload_state") == "queued"
                and part.get("youtube_video_id") is None
                and part.get("reason") is None
                and states[index] == "queued"
                and failure_kinds[index] == ""
                and completion_reasons[index] == ""
                and recovery_reasons[index] == ""
                and not retry_ids[index]
            )
            if first_queued is None:
                if confirmed:
                    continue
                if not queued:
                    raise AutoYouTubeExecutionError("continuation_not_allowed")
                first_queued = index
            elif not queued:
                raise AutoYouTubeExecutionError("continuation_not_allowed")
        if first_queued is None or first_queued == 0:
            raise AutoYouTubeExecutionError("continuation_not_allowed")
        return job, record, descriptors, first_queued

    def _validate_unique_lineage(
        self, job_id: str, job: Mapping[str, Any]
    ) -> None:
        lineage = [
            candidate
            for candidate in self._job_manager.snapshot_jobs()
            if candidate.get("origin") == "auto_youtube"
            and candidate.get("auto_youtube_key")
            == job.get("auto_youtube_key")
        ]
        if (
            len(lineage) != 1
            or str(lineage[0].get("id") or "") != str(job_id)
        ):
            raise AutoYouTubeExecutionError("conflicting_ownership")

    def _uncertain_recovery_candidate(
        self,
        job_id: str,
        item_id: str,
        *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> tuple[
        Mapping[str, Any], Mapping[str, Any], list[Dict[str, Any]], int
    ]:
        job, record, descriptors = self._ownership(job_id, records=records)
        self._validate_unique_lineage(job_id, job)
        item_ids = list(job.get("item_ids") or [])
        try:
            index = item_ids.index(str(item_id))
        except ValueError as exc:
            raise AutoYouTubeExecutionError("ownership_mismatch") from exc
        parts = list(record.get("parts") or [])
        states = list(job.get("item_states") or [])
        failure_kinds = list(job.get("item_failure_kinds") or [])
        completion_reasons = list(job.get("item_completion_reasons") or [])
        recovery_reasons = list(job.get("item_recovery_reasons") or [])
        if not (
            len(parts)
            == len(item_ids)
            == len(states)
            == len(failure_kinds)
            == len(completion_reasons)
            == len(recovery_reasons)
        ) or index >= len(parts):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        part = parts[index]
        if part.get("youtube_video_id") is not None:
            raise AutoYouTubeExecutionError("video_already_confirmed")
        reason = str(
            recovery_reasons[index] or completion_reasons[index] or ""
        )
        if (
            job.get("execution_deferred") is not True
            or record.get("state") != "needs_attention"
            or record.get("reason") != "upload_outcome_uncertain"
            or states[index] != "failed"
            or failure_kinds[index] != "uncertain"
            or reason != "upload_outcome_uncertain"
            or part.get("upload_item_id") != str(item_id)
            or part.get("upload_state") != "uncertain"
            or part.get("reason") != "upload_outcome_uncertain"
        ):
            raise AutoYouTubeExecutionError("recovery_not_allowed")
        for position, candidate in enumerate(parts):
            if position < index:
                if (
                    candidate.get("upload_state")
                    not in {"video_confirmed", "completed"}
                    or not candidate.get("youtube_video_id")
                    or states[position] != "completed"
                ):
                    raise AutoYouTubeExecutionError("ownership_mismatch")
            elif position > index and (
                candidate.get("upload_state") != "queued"
                or candidate.get("youtube_video_id") is not None
                or states[position] != "queued"
            ):
                raise AutoYouTubeExecutionError("ownership_mismatch")
        return job, record, descriptors, index

    def _known_pretransfer_recovery_candidate(
        self,
        job_id: str,
        item_id: str,
        *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> tuple[
        Mapping[str, Any], Mapping[str, Any], list[Dict[str, Any]], int, str
    ]:
        job, record, descriptors = self._ownership(job_id, records=records)
        self._validate_unique_lineage(job_id, job)
        item_ids = list(job.get("item_ids") or [])
        try:
            index = item_ids.index(str(item_id))
        except ValueError as exc:
            raise AutoYouTubeExecutionError("ownership_mismatch") from exc
        parts = list(record.get("parts") or [])
        states = list(job.get("item_states") or [])
        failure_kinds = list(job.get("item_failure_kinds") or [])
        completion_reasons = list(job.get("item_completion_reasons") or [])
        recovery_reasons = list(job.get("item_recovery_reasons") or [])
        if not (
            len(parts)
            == len(item_ids)
            == len(states)
            == len(failure_kinds)
            == len(completion_reasons)
            == len(recovery_reasons)
        ) or index >= len(parts):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        part = parts[index]
        reason = str(recovery_reasons[index] or completion_reasons[index] or "")
        if part.get("youtube_video_id") is not None:
            raise AutoYouTubeExecutionError("video_already_confirmed")
        if (
            reason not in SAFE_KNOWN_PRETRANSFER_RECOVERY_REASONS
            or job.get("execution_deferred") is not True
            or record.get("state") != "needs_attention"
            or record.get("reason") != reason
            or states[index] != "failed"
            or failure_kinds[index] != "known"
            or part.get("upload_item_id") != str(item_id)
            or part.get("upload_state") != "failed_known"
            or part.get("reason") != reason
            or part.get("attempts") != 0
        ):
            raise AutoYouTubeExecutionError("known_recovery_not_allowed")
        for position, candidate in enumerate(parts):
            if position < index:
                if (
                    candidate.get("upload_state")
                    not in {"video_confirmed", "completed"}
                    or not candidate.get("youtube_video_id")
                    or states[position] != "completed"
                ):
                    raise AutoYouTubeExecutionError("ownership_mismatch")
            elif position > index and (
                candidate.get("upload_state") != "queued"
                or candidate.get("youtube_video_id") is not None
                or states[position] != "queued"
            ):
                raise AutoYouTubeExecutionError("ownership_mismatch")
        return job, record, descriptors, index, reason

    @staticmethod
    def _possible_uncertain_item_ids(snapshot: Mapping[str, Any]) -> list[str]:
        if snapshot.get("execution_deferred") is not True:
            return []
        item_ids = list(snapshot.get("item_ids") or [])
        states = list(snapshot.get("item_states") or [])
        failure_kinds = list(snapshot.get("item_failure_kinds") or [])
        completion_reasons = list(snapshot.get("item_completion_reasons") or [])
        recovery_reasons = list(snapshot.get("item_recovery_reasons") or [])
        if not (
            len(item_ids)
            == len(states)
            == len(failure_kinds)
            == len(completion_reasons)
            == len(recovery_reasons)
        ):
            return []
        return [
            str(item_id)
            for item_id, state, failure_kind, completion_reason, recovery_reason in zip(
                item_ids,
                states,
                failure_kinds,
                completion_reasons,
                recovery_reasons,
            )
            if (
                state == "failed"
                and failure_kind == "uncertain"
                and str(recovery_reason or completion_reason or "")
                == "upload_outcome_uncertain"
            )
        ]

    @staticmethod
    def _possible_known_pretransfer_item_ids(
        snapshot: Mapping[str, Any]
    ) -> list[str]:
        if snapshot.get("execution_deferred") is not True:
            return []
        item_ids = list(snapshot.get("item_ids") or [])
        states = list(snapshot.get("item_states") or [])
        failure_kinds = list(snapshot.get("item_failure_kinds") or [])
        completion_reasons = list(snapshot.get("item_completion_reasons") or [])
        recovery_reasons = list(snapshot.get("item_recovery_reasons") or [])
        if not (
            len(item_ids)
            == len(states)
            == len(failure_kinds)
            == len(completion_reasons)
            == len(recovery_reasons)
        ):
            return []
        return [
            str(item_id)
            for item_id, state, failure_kind, completion_reason, recovery_reason in zip(
                item_ids,
                states,
                failure_kinds,
                completion_reasons,
                recovery_reasons,
            )
            if (
                state == "failed"
                and failure_kind == "known"
                and str(recovery_reason or completion_reason or "")
                in SAFE_KNOWN_PRETRANSFER_RECOVERY_REASONS
            )
        ]

    def recovery_status_for_jobs(
        self,
        jobs: list[Mapping[str, Any]],
        *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return read-only, ledger-backed eligibility for Queue rendering."""
        if records is None:
            try:
                records = self._state_store.list_records()
            except Exception:
                return {}
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for snapshot in jobs:
            if snapshot.get("origin") != "auto_youtube":
                continue
            job_id = str(snapshot.get("id") or "")
            eligible: list[str] = []
            for item_id in self._possible_uncertain_item_ids(snapshot):
                try:
                    self._uncertain_recovery_candidate(
                        job_id, item_id, records=records
                    )
                except AutoYouTubeExecutionError as exc:
                    if exc.code != "recovery_not_allowed":
                        continue
                    try:
                        _job, legacy_record, index = self._already_uploaded_candidate(
                            job_id, item_id, records=records
                        )
                        if legacy_record["parts"][index]["upload_state"] != "queued":
                            continue
                    except Exception:
                        continue
                eligible.append(str(item_id))
            if eligible:
                result[job_id] = {
                    "reason": "upload_outcome_uncertain",
                    "eligible_item_ids": eligible,
                }
            confirmed_eligible: list[str] = []
            for item_id in self._possible_uncertain_item_ids(snapshot):
                try:
                    self._already_uploaded_candidate(
                        job_id, item_id, records=records
                    )
                except Exception:
                    continue
                confirmed_eligible.append(str(item_id))
            if confirmed_eligible:
                result.setdefault(job_id, {})["already_uploaded_eligible_item_ids"] = confirmed_eligible
            known_eligible: list[str] = []
            for item_id in self._possible_known_pretransfer_item_ids(snapshot):
                try:
                    self._known_pretransfer_recovery_candidate(
                        job_id, item_id, records=records
                    )
                except Exception:
                    continue
                known_eligible.append(item_id)
            if known_eligible:
                result.setdefault(job_id, {})["known_eligible_item_ids"] = (
                    known_eligible
                )
            try:
                _job, _record, _descriptors, next_index = (
                    self._continuation_candidate(job_id, records=records)
                )
            except Exception:
                pass
            else:
                result.setdefault(job_id, {})["continuation"] = {
                    "eligible": True,
                    "next_item_id": str(snapshot["item_ids"][next_index]),
                    "remaining_part_count": len(snapshot["item_ids"]) - next_index,
                }
        return result

    def recover_uncertain_part_for_execution(
        self, job_id: str, item_id: str
    ) -> Dict[str, Any]:
        """Durably authorize one reviewed uncertain part, then release its job."""
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubeExecutionError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubeExecutionError("job_store_unavailable")
        legacy = False
        try:
            job, record, descriptors, index = self._uncertain_recovery_candidate(
                str(job_id), str(item_id)
            )
        except AutoYouTubeExecutionError as exc:
            if exc.code != "recovery_not_allowed":
                raise
            try:
                job, record, index = self._already_uploaded_candidate(str(job_id), str(item_id))
            except AutoYouTubeExecutionError:
                raise exc
            if record["parts"][index]["upload_state"] != "queued":
                raise exc
            descriptors = self._materializer()._part_descriptors(record)
            legacy = True
        try:
            self._materializer()._validate_media(record, descriptors)
        except (_MissingMaterializationMedia, _InvalidMaterializationMedia) as exc:
            raise AutoYouTubeExecutionError("recovery_media_invalid") from exc
        if not self._job_manager.stage_uncertain_auto_youtube_item_recovery(
            str(job_id), str(item_id)
        ):
            raise AutoYouTubeExecutionError("recovery_not_allowed")
        try:
            recover = (
                self._state_store.recover_reviewed_legacy_queued_part
                if legacy else self._state_store.recover_uncertain_part
            )
            recovered = recover(
                record["streamer"],
                record["twitch_vod_id"],
                upload_job_id=str(job_id),
                upload_item_id=str(item_id),
                part_index=index + 1,
            )
        except YouTubeUploadStateValidationError as exc:
            try:
                self._job_manager.block_auto_youtube_item(
                    str(job_id),
                    str(item_id),
                    uncertain=True,
                    reason="upload_outcome_uncertain",
                )
            except Exception:
                pass
            raise AutoYouTubeExecutionError("recovery_not_allowed") from exc
        except YouTubeUploadStatePersistenceError as exc:
            try:
                self._job_manager.block_auto_youtube_item(
                    str(job_id),
                    str(item_id),
                    uncertain=True,
                    reason="upload_outcome_uncertain",
                )
            except Exception:
                pass
            raise AutoYouTubeExecutionError(
                "recovery_persistence_failed"
            ) from exc

        current = self._job_manager.get_job(str(job_id)) or {}
        current_states = list(current.get("item_states") or [])
        recovered_parts = list(recovered.get("parts") or [])
        aligned = len(current_states) == len(recovered_parts)
        if aligned:
            for position, part in enumerate(recovered_parts):
                if position < index:
                    valid = (
                        part.get("upload_state")
                        in {"video_confirmed", "completed"}
                        and bool(part.get("youtube_video_id"))
                        and current_states[position] == "completed"
                    )
                else:
                    valid = (
                        part.get("upload_state") == "queued"
                        and part.get("youtube_video_id") is None
                        and current_states[position] == "queued"
                    )
                if not valid:
                    aligned = False
                    break
        if (
            current.get("execution_deferred") is not True
            or recovered.get("state") != "upload_queued"
            or not aligned
        ):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        if not self._job_manager.release_auto_youtube_job_for_execution(
            str(job_id)
        ):
            raise AutoYouTubeExecutionError("recovery_not_allowed")
        self._log(
            str(job_id),
            f"Auto YouTube part {index + 1}/{len(recovered_parts)} was requeued "
            "after explicit YouTube Studio review.",
        )
        return {
            "job_id": str(job_id),
            "item_id": str(item_id),
            "part_index": index + 1,
        }

    def _already_uploaded_candidate(
        self, job_id: str, item_id: str, *,
        records: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], int]:
        job, record, _descriptors = self._ownership(str(job_id), records=records)
        self._validate_unique_lineage(str(job_id), job)
        item_ids = list(job.get("item_ids") or [])
        try:
            index = item_ids.index(str(item_id))
        except ValueError as exc:
            raise AutoYouTubeExecutionError("ownership_mismatch") from exc
        parts = list(record.get("parts") or [])
        states = list(job.get("item_states") or [])
        kinds = list(job.get("item_failure_kinds") or [])
        completion = list(job.get("item_completion_reasons") or [])
        recovery = list(job.get("item_recovery_reasons") or [])
        retries = list(job.get("item_retry_job_ids") or [])
        if not (len(parts) == len(item_ids) == len(states) == len(kinds)
                == len(completion) == len(recovery) == len(retries)):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        part = parts[index]
        normal_uncertain = (
            record.get("state") == "needs_attention"
            and record.get("reason") == "upload_outcome_uncertain"
            and part.get("upload_state") == "uncertain"
            and part.get("reason") == "upload_outcome_uncertain"
        )
        legacy_queued = (
            (
                record.get("state") == "upload_queued"
                or (record.get("state") == "needs_attention"
                    and record.get("reason") == "upload_outcome_uncertain")
            )
            and part.get("upload_state") == "queued"
            and part.get("attempts") == 0
            and part.get("reason") is None
        )
        if (
            job.get("execution_deferred") is not True
            or states[index] != "failed"
            or kinds[index] != "uncertain"
            or str(recovery[index] or completion[index] or "") != "upload_outcome_uncertain"
            or retries[index]
            or part.get("upload_item_id") != str(item_id)
            or part.get("youtube_video_id") is not None
            or not (normal_uncertain or legacy_queued)
        ):
            raise AutoYouTubeExecutionError("confirmation_not_allowed")
        for position, candidate in enumerate(parts):
            if position < index and (
                candidate.get("upload_state") not in {"video_confirmed", "completed"}
                or not candidate.get("youtube_video_id")
                or states[position] != "completed"
            ):
                raise AutoYouTubeExecutionError("ownership_mismatch")
            if position > index and (
                candidate.get("upload_state") != "queued"
                or candidate.get("youtube_video_id") is not None
                or states[position] != "queued"
            ):
                raise AutoYouTubeExecutionError("ownership_mismatch")
        return job, record, index

    def confirm_already_uploaded_part(
        self, job_id: str, item_id: str, video_input: str
    ) -> Dict[str, Any]:
        """Resolve one uncertain item from verified remote ownership; never release work."""
        video_id = youtube_video_id_from_input(video_input)
        if video_id is None:
            raise AutoYouTubeExecutionError("invalid_youtube_video_id")
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubeExecutionError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubeExecutionError("job_store_unavailable")
        job, record, index = self._already_uploaded_candidate(str(job_id), str(item_id))
        parts = list(record["parts"])
        try:
            service = self._service_getter(dict(self._settings_provider()), interactive=False)
            verified = youtube_video_belongs_to_connected_channel(service, video_id)
        except Exception as exc:
            raise AutoYouTubeExecutionError("video_verification_unavailable") from exc
        if not verified:
            raise AutoYouTubeExecutionError("video_not_confirmed")
        with self._job_manager.lock:
            # The remote lookup is slow; recheck the item under the Queue lock
            # so a concurrent Retry cannot release it between review and save.
            self._already_uploaded_candidate(str(job_id), str(item_id))
            try:
                confirmed = self._state_store.confirm_reviewed_part_video(
                    record["streamer"], record["twitch_vod_id"],
                    upload_job_id=str(job_id), upload_item_id=str(item_id),
                    part_index=index + 1, youtube_video_id=video_id,
                )
            except YouTubeUploadStateValidationError as exc:
                raise AutoYouTubeExecutionError("confirmation_not_allowed") from exc
            except YouTubeUploadStatePersistenceError as exc:
                raise AutoYouTubeExecutionError("confirmation_persistence_failed") from exc
            if not self._job_manager.complete_auto_youtube_item(str(job_id), str(item_id)):
                raise AutoYouTubeExecutionError("ownership_mismatch")
        self._log(str(job_id), f"Auto YouTube part {index + 1}/{len(parts)} confirmed from YouTube Studio review.")
        return {
            "job_id": str(job_id), "item_id": str(item_id),
            "part_index": index + 1, "youtube_video_id": video_id,
            "bundle_state": confirmed["state"],
        }

    def recover_known_pretransfer_part_for_execution(
        self, job_id: str, item_id: str
    ) -> Dict[str, Any]:
        """Durably retry one proven pre-transfer known failure."""
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubeExecutionError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubeExecutionError("job_store_unavailable")
        job, record, descriptors, index, reason = (
            self._known_pretransfer_recovery_candidate(
                str(job_id), str(item_id)
            )
        )
        try:
            self._materializer()._validate_media(record, descriptors)
        except (_MissingMaterializationMedia, _InvalidMaterializationMedia) as exc:
            raise AutoYouTubeExecutionError(
                "known_recovery_media_invalid"
            ) from exc
        if not self._job_manager.stage_known_auto_youtube_item_recovery(
            str(job_id), str(item_id), reason=reason
        ):
            raise AutoYouTubeExecutionError("known_recovery_not_allowed")
        try:
            recovered = self._state_store.recover_known_pretransfer_part(
                record["streamer"],
                record["twitch_vod_id"],
                upload_job_id=str(job_id),
                upload_item_id=str(item_id),
                part_index=index + 1,
                reason=reason,
            )
        except YouTubeUploadStateValidationError as exc:
            try:
                self._job_manager.block_auto_youtube_item(
                    str(job_id),
                    str(item_id),
                    uncertain=False,
                    reason=reason,
                )
            except Exception:
                pass
            raise AutoYouTubeExecutionError(
                "known_recovery_not_allowed"
            ) from exc
        except YouTubeUploadStatePersistenceError as exc:
            try:
                self._job_manager.block_auto_youtube_item(
                    str(job_id),
                    str(item_id),
                    uncertain=False,
                    reason=reason,
                )
            except Exception:
                pass
            raise AutoYouTubeExecutionError(
                "known_recovery_persistence_failed"
            ) from exc

        current = self._job_manager.get_job(str(job_id)) or {}
        current_states = list(current.get("item_states") or [])
        recovered_parts = list(recovered.get("parts") or [])
        aligned = len(current_states) == len(recovered_parts)
        if aligned:
            for position, part in enumerate(recovered_parts):
                if position < index:
                    valid = (
                        part.get("upload_state")
                        in {"video_confirmed", "completed"}
                        and bool(part.get("youtube_video_id"))
                        and current_states[position] == "completed"
                    )
                else:
                    valid = (
                        part.get("upload_state") == "queued"
                        and part.get("youtube_video_id") is None
                        and current_states[position] == "queued"
                    )
                if not valid:
                    aligned = False
                    break
        if (
            current.get("execution_deferred") is not True
            or recovered.get("state") != "upload_queued"
            or not aligned
        ):
            raise AutoYouTubeExecutionError("ownership_mismatch")
        if not self._job_manager.release_auto_youtube_job_for_execution(
            str(job_id)
        ):
            raise AutoYouTubeExecutionError("known_recovery_not_allowed")
        self._log(
            str(job_id),
            f"Auto YouTube part {index + 1}/{len(recovered_parts)} was requeued "
            "after a known pre-transfer failure.",
        )
        return {
            "job_id": str(job_id),
            "item_id": str(item_id),
            "part_index": index + 1,
        }

    def release_auto_youtube_job_for_execution(self, job_id: str) -> bool:
        """Validate ownership, then durably clear the existing execution gate."""
        self._validate_release_candidate(str(job_id), deferred=True)
        if not self._job_manager.release_auto_youtube_job_for_execution(str(job_id)):
            raise AutoYouTubeExecutionError("release_not_allowed")
        return True

    def continue_confirmed_auto_youtube_job_for_execution(
        self, job_id: str
    ) -> Dict[str, Any]:
        """Explicitly release only the queued suffix after reviewed confirmation."""
        if self._state_store.health().get("healthy") is not True:
            raise AutoYouTubeExecutionError("ownership_store_unavailable")
        health = self._job_manager.persistence_status()
        if health.get("enabled") is not True or health.get("healthy") is not True:
            raise AutoYouTubeExecutionError("job_store_unavailable")
        job, record, descriptors, next_index = self._continuation_candidate(
            str(job_id)
        )
        try:
            self._materializer()._validate_media(record, descriptors)
        except (_MissingMaterializationMedia, _InvalidMaterializationMedia) as exc:
            raise AutoYouTubeExecutionError("continuation_media_invalid") from exc
        if not self._job_manager.release_auto_youtube_job_for_execution(
            str(job_id)
        ):
            raise AutoYouTubeExecutionError("continuation_not_allowed")
        self._log(
            str(job_id),
            f"Auto YouTube remaining upload parts continue at part "
            f"{next_index + 1}/{len(descriptors)} after reviewed confirmation.",
        )
        return {
            "job_id": str(job_id),
            "next_item_id": str(job["item_ids"][next_index]),
            "next_part_index": next_index + 1,
            "remaining_part_count": len(descriptors) - next_index,
        }

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
                    try:
                        self._continuation_candidate(job_id, records=records)
                    except AutoYouTubeExecutionError:
                        self.release_auto_youtube_job_for_execution(job_id)
                    else:
                        # A reviewed remote confirmation intentionally requires
                        # an administrator to approve the remaining suffix.
                        result["ignored"] += 1
                        continue
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
            failure_kinds = list(job.get("item_failure_kinds") or [])
            recovery_reasons = list(job.get("item_recovery_reasons") or [])
            completion_reasons = list(job.get("item_completion_reasons") or [])
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
                if (
                    ledger_state == "queued"
                    and job_state == "failed"
                    and index < len(failure_kinds)
                    and index < len(recovery_reasons)
                    and index < len(completion_reasons)
                    and failure_kinds[index] == "uncertain"
                    and str(
                        recovery_reasons[index]
                        or completion_reasons[index]
                        or ""
                    ) == "upload_outcome_uncertain"
                ):
                    # A prior lost update can erase transfer_started from the
                    # ledger. The durable JobStore uncertainty still forbids
                    # automatic requeue/reupload until explicit review.
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
