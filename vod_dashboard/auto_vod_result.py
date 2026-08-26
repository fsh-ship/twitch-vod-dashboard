"""Validation of one exact completed Auto VOD media result."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.twitch import extract_twitch_vod_id


def resolve_completed_auto_vod_output(
    raw_path: Any,
    settings: Mapping[str, Any],
    expected_twitch_vod_id: Any,
    *,
    media_policy: MediaPathPolicy,
) -> Dict[str, Any]:
    """Verify yt-dlp's exact final path and its adjacent trusted info JSON."""
    expected = str(expected_twitch_vod_id or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{5,31}", expected):
        raise RuntimeError("Invalid expected Twitch VOD identity.")
    path = media_policy.safe_local_video_path(raw_path, settings, must_exist=True)
    info_path = path.with_suffix(".info.json")
    try:
        info_path = media_policy.resolve_media_path(
            info_path, must_exist=True, require_file=True
        )
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Final Auto VOD metadata could not be verified.") from exc
    if extract_twitch_vod_id(info) != expected:
        raise RuntimeError("Final Auto VOD identity does not match the job.")
    size_bytes = path.stat().st_size
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise RuntimeError("Final Auto VOD size could not be verified.")
    return {
        "completed_media_path": path.relative_to(
            media_policy.media_root
        ).as_posix(),
        "completed_media_size_bytes": size_bytes,
        "completed_twitch_vod_id": expected,
    }
