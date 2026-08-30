"""Product-level automation policy compatibility helpers.

This module deliberately contains no persistence or runtime coordination.  It
translates the existing compact per-streamer settings into user-intent concepts
without consulting global pause controls, starting work, or repairing legacy
values.  The current persisted field names remain the source of truth.

Important compatibility facts encoded here:

* ``auto_youtube_upload=True`` without ``auto_vod_download=True`` is preserved
  and reported as requiring review.
* An Auto YouTube playlist is optional.  A configured but unavailable playlist
  can be reported as an unavailable dependency when a caller has verified that
  condition.
* Automatic cleanup delay ``0`` means no automatic cleanup; it is distinct
  from the legacy ``move_uploaded_vods`` archive behavior.
* ``youtube_auto_upload`` belongs to the manual-download workflow and is not a
  VOD Handling mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from vod_dashboard.settings import (
    AUTO_YOUTUBE_CLEANUP_DELAY_HOURS,
    canonical_streamer_login,
    normalize_streamer_profiles,
)


VOD_MANUAL = "manual"
VOD_AUTO_DOWNLOAD = "auto_download"
VOD_DOWNLOAD_AND_YOUTUBE = "download_and_youtube"
VOD_NEEDS_REVIEW = "needs_review"
VOD_HANDLING_MODES = frozenset(
    {VOD_MANUAL, VOD_AUTO_DOWNLOAD, VOD_DOWNLOAD_AND_YOUTUBE}
)

LIVE_MANUAL = "manual"
LIVE_AUTOMATIC = "automatic"
LIVE_RECORDING_MODES = frozenset({LIVE_MANUAL, LIVE_AUTOMATIC})

RETENTION_KEEP_LOCAL = "keep_local"
RETENTION_CLEANUP_AFTER_DELAY = "cleanup_after_delay"
RETENTION_MODES = frozenset(
    {RETENTION_KEEP_LOCAL, RETENTION_CLEANUP_AFTER_DELAY}
)

VALID = "valid"
NEEDS_REVIEW = "needs_review"
UNAVAILABLE_DEPENDENCY = "unavailable_dependency"


@dataclass(frozen=True)
class PolicyValidation:
    """A stable validation result for later Settings UI rendering."""

    state: str
    issues: Tuple[str, ...] = ()


@dataclass(frozen=True)
class VodHandlingPolicy:
    """Derived VOD intent plus the exact effective legacy flags."""

    mode: str
    auto_vod_download: bool
    auto_youtube_upload: bool
    validation: PolicyValidation


@dataclass(frozen=True)
class StreamerAutomationPolicy:
    """Independent product dimensions for one streamer profile."""

    vod_handling: VodHandlingPolicy
    live_recording: str
    playlist_id: str
    validation: PolicyValidation


@dataclass(frozen=True)
class RetentionPolicy:
    """Default Auto YouTube retention, separate from legacy archiving."""

    mode: str
    delay_hours: Optional[int]
    validation: PolicyValidation


@dataclass(frozen=True)
class ManualDownloadWorkflow:
    """Compatibility view of the legacy post-manual-download upload path."""

    requested: bool
    legacy_youtube_gate_enabled: bool
    enabled: bool
    status: str


def derive_vod_handling(
    auto_vod_download: Any, auto_youtube_upload: Any
) -> VodHandlingPolicy:
    """Translate strict persisted flags without mutating or repairing them."""
    auto_vod = auto_vod_download is True
    auto_youtube = auto_youtube_upload is True
    if not auto_vod and not auto_youtube:
        return VodHandlingPolicy(
            VOD_MANUAL, False, False, PolicyValidation(VALID)
        )
    if auto_vod and not auto_youtube:
        return VodHandlingPolicy(
            VOD_AUTO_DOWNLOAD, True, False, PolicyValidation(VALID)
        )
    if auto_vod and auto_youtube:
        return VodHandlingPolicy(
            VOD_DOWNLOAD_AND_YOUTUBE,
            True,
            True,
            PolicyValidation(VALID),
        )
    return VodHandlingPolicy(
        VOD_NEEDS_REVIEW,
        False,
        True,
        PolicyValidation(NEEDS_REVIEW, ("auto_youtube_requires_auto_vod",)),
    )


def vod_handling_from_profile(
    profile: Optional[Mapping[str, Any]],
) -> VodHandlingPolicy:
    """Derive VOD intent from one raw or normalized streamer profile."""
    source = profile if isinstance(profile, Mapping) else {}
    return derive_vod_handling(
        source.get("auto_vod_download"),
        source.get("auto_youtube_upload"),
    )


def apply_vod_handling(
    existing_profile: Optional[Mapping[str, Any]], mode: str
) -> Dict[str, Any]:
    """Return an explicitly resolved compact profile for one valid mode.

    Calling this function represents an explicit user decision.  Merely
    deriving a policy never calls it, so legacy-invalid flags remain intact.
    Unrelated profile fields are copied unchanged.
    """
    if mode not in VOD_HANDLING_MODES:
        raise ValueError("unsupported_vod_handling")
    updated = (
        dict(existing_profile)
        if isinstance(existing_profile, Mapping)
        else {}
    )
    updated.pop("auto_vod_download", None)
    updated.pop("auto_youtube_upload", None)
    if mode in {VOD_AUTO_DOWNLOAD, VOD_DOWNLOAD_AND_YOUTUBE}:
        updated["auto_vod_download"] = True
    if mode == VOD_DOWNLOAD_AND_YOUTUBE:
        updated["auto_youtube_upload"] = True
    return updated


def derive_live_recording(auto_record: Any) -> str:
    """Map the independent strict per-streamer recording flag."""
    return LIVE_AUTOMATIC if auto_record is True else LIVE_MANUAL


def apply_live_recording(
    existing_profile: Optional[Mapping[str, Any]], mode: str
) -> Dict[str, Any]:
    """Return a copied profile with only the live-recording policy changed."""
    if mode not in LIVE_RECORDING_MODES:
        raise ValueError("unsupported_live_recording")
    updated = (
        dict(existing_profile)
        if isinstance(existing_profile, Mapping)
        else {}
    )
    updated.pop("auto_record", None)
    if mode == LIVE_AUTOMATIC:
        updated["auto_record"] = True
    return updated


def validate_streamer_automation(
    profile: Optional[Mapping[str, Any]],
    *,
    youtube_dependency_available: Optional[bool] = None,
    playlist_dependency_available: Optional[bool] = None,
) -> StreamerAutomationPolicy:
    """Validate policy shape separately from operational global pauses.

    Dependency arguments are optional because settings alone cannot prove
    connection or playlist availability.  A blank playlist is valid under the
    current Auto YouTube lifecycle and therefore never creates an issue.
    """
    source = profile if isinstance(profile, Mapping) else {}
    vod = vod_handling_from_profile(source)
    playlist_id = str(source.get("youtube_playlist_id") or "").strip()
    validation = vod.validation
    if validation.state == VALID and vod.mode == VOD_DOWNLOAD_AND_YOUTUBE:
        issues = []
        if youtube_dependency_available is False:
            issues.append("youtube_unavailable")
        if playlist_id and playlist_dependency_available is False:
            issues.append("playlist_unavailable")
        if issues:
            validation = PolicyValidation(
                UNAVAILABLE_DEPENDENCY, tuple(issues)
            )
    return StreamerAutomationPolicy(
        vod_handling=vod,
        live_recording=derive_live_recording(source.get("auto_record")),
        playlist_id=playlist_id,
        validation=validation,
    )


def derive_retention(delay_hours: Any) -> RetentionPolicy:
    """Translate the global default automatic-cleanup delay exactly."""
    if type(delay_hours) is int and delay_hours == 0:
        return RetentionPolicy(
            RETENTION_KEEP_LOCAL, None, PolicyValidation(VALID)
        )
    if (
        type(delay_hours) is int
        and delay_hours in AUTO_YOUTUBE_CLEANUP_DELAY_HOURS
    ):
        return RetentionPolicy(
            RETENTION_CLEANUP_AFTER_DELAY,
            delay_hours,
            PolicyValidation(VALID),
        )
    return RetentionPolicy(
        NEEDS_REVIEW,
        None,
        PolicyValidation(NEEDS_REVIEW, ("unsupported_retention_delay",)),
    )


def retention_delay_for_mode(
    mode: str, delay_hours: Optional[int] = None
) -> int:
    """Map an explicit product retention choice to the persisted delay."""
    if mode == RETENTION_KEEP_LOCAL:
        return 0
    if (
        mode == RETENTION_CLEANUP_AFTER_DELAY
        and type(delay_hours) is int
        and delay_hours in AUTO_YOUTUBE_CLEANUP_DELAY_HOURS
    ):
        return delay_hours
    raise ValueError("unsupported_retention")


def derive_manual_download_workflow(
    youtube_enabled: Any, youtube_auto_upload: Any
) -> ManualDownloadWorkflow:
    """Classify the legacy manual-download follow-up without merging it."""
    gate_enabled = youtube_enabled is True
    requested = youtube_auto_upload is True
    enabled = gate_enabled and requested
    if enabled:
        status = "enabled"
    elif requested:
        status = "blocked_by_legacy_youtube_gate"
    else:
        status = "disabled"
    return ManualDownloadWorkflow(
        requested=requested,
        legacy_youtube_gate_enabled=gate_enabled,
        enabled=enabled,
        status=status,
    )


def summarize_vod_handling(
    configured_streamers: Iterable[Any],
    streamer_profiles: Any,
) -> Dict[str, int]:
    """Count configured streamers by product policy for future UI summaries."""
    profiles = normalize_streamer_profiles(streamer_profiles)
    counts = {
        VOD_MANUAL: 0,
        VOD_AUTO_DOWNLOAD: 0,
        VOD_DOWNLOAD_AND_YOUTUBE: 0,
        VOD_NEEDS_REVIEW: 0,
    }
    seen = set()
    for raw_streamer in configured_streamers:
        streamer = canonical_streamer_login(raw_streamer)
        if not streamer or streamer in seen:
            continue
        seen.add(streamer)
        mode = vod_handling_from_profile(profiles.get(streamer)).mode
        counts[mode] += 1
    return counts
