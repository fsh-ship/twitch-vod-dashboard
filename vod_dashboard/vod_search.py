"""Flask-independent VOD search input and download selection logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from vod_dashboard.auto_vod import normalize_auto_vod_id
from vod_dashboard.settings import canonical_streamer_login


@dataclass(frozen=True)
class DownloadSelection:
    urls: List[str]
    label: Any
    error: Optional[Dict[str, Any]] = None


def search_vods_from_payload(
    data: Mapping[str, Any],
    settings: Dict[str, Any],
    default_streamers: Iterable[Any],
    known_vod_ids: set[str],
    *,
    date_parser: Callable[[Any], Any],
    integer_parser: Callable[[Any, int], int],
    search_service: Callable[..., Dict[str, List[Dict[str, Any]]]],
    source_runner: Callable[..., List[Dict[str, Any]]],
    detail_runner: Callable[..., Dict[str, Any]],
    log_callback: Callable[[str], None],
) -> Dict[str, List[Dict[str, Any]]]:
    """Resolve the existing search payload semantics and run the search service."""
    streamers = data.get("streamers") or default_streamers
    if isinstance(streamers, str):
        streamers = [streamers]
    streamers = [
        str(streamer).strip().lstrip("@")
        for streamer in streamers
        if str(streamer).strip()
    ]
    start = date_parser(data.get("from"))
    end = date_parser(data.get("to"))
    limit = max(
        1,
        integer_parser(
            data.get("limit") or settings["playlist_end"],
            settings["playlist_end"],
        ),
    )
    include_unknown = bool(
        data.get(
            "include_unknown_dates",
            settings.get("include_unknown_dates", True),
        )
    )
    strict_date_filter = bool(
        data.get(
            "strict_date_filter",
            settings.get("strict_date_filter", False),
        )
    )
    exclude_live = bool(
        data.get(
            "exclude_live_streams",
            settings.get("exclude_live_streams", True),
        )
    )
    only_real_vods = bool(
        data.get(
            "only_real_vod_urls",
            settings.get("only_real_vod_urls", True),
        )
    )
    return search_service(
        streamers,
        settings,
        known_vod_ids,
        start,
        end,
        limit,
        include_unknown,
        strict_date_filter,
        exclude_live,
        only_real_vods,
        source_runner=source_runner,
        detail_runner=detail_runner,
        log_callback=log_callback,
    )


def apply_auto_vod_baseline_status(
    payload: Dict[str, List[Dict[str, Any]]],
    baseline_vod_ids: Mapping[str, set[str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Add the compact search-display flag for intentional Auto VOD baselines."""
    results = payload.get("results")
    if not isinstance(results, list) or not baseline_vod_ids:
        return payload

    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("already_downloaded") is True:
            continue
        streamer = canonical_streamer_login(result.get("streamer"))
        vod_id = normalize_auto_vod_id(result.get("id"))
        if streamer and vod_id and vod_id in baseline_vod_ids.get(streamer, set()):
            result["auto_vod_baseline_existing"] = True
    return payload


def prepare_download_selection(
    data: Mapping[str, Any],
    *,
    validator: Callable[[Any], Dict[str, Any]],
) -> DownloadSelection:
    """Validate and deduplicate the VOD URLs selected for a download job."""
    single_url = data.get("url") or data.get("vod_url")
    if single_url:
        check = validator(single_url)
        if not check.get("ok"):
            return DownloadSelection([], None, check)
        urls = [check["url"]]
        label = data.get("label") or f"Single VOD {check.get('vod_id') or ''}".strip()
    else:
        urls = data.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]

        normalized = []
        for raw in urls:
            raw = str(raw or "").strip()
            if not raw:
                continue
            check = validator(raw)
            if not check.get("ok"):
                return DownloadSelection([], None, check)
            normalized.append(check["url"])

        urls = list(dict.fromkeys(normalized))
        label = data.get("label") or f"{len(urls)} VOD(s)"

    if not urls:
        return DownloadSelection(
            [],
            label,
            {"ok": False, "error": "No valid VOD URLs were provided."},
        )
    return DownloadSelection(urls, label)
