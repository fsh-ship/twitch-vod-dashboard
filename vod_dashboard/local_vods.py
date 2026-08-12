from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Set

from vod_dashboard.media import MediaPathPolicy, VIDEO_EXTENSIONS


def local_video_metadata_payload(
    path: Path,
    settings: Mapping[str, Any],
    uploaded_set: Set[str],
    *,
    media_policy: MediaPathPolicy,
    download_root: Path,
    uploaded_root: Path,
    metadata_loader: Callable[[Path, Mapping[str, Any]], Dict[str, Any]],
    youtube_metadata_builder: Callable[
        [Path, Mapping[str, Any]], Dict[str, Any]
    ],
    marker_reader: Callable[[Path], Dict[str, Any]],
    marker_path_builder: Callable[[Path], Path],
    sidecar_loader: Callable[[Path], List[Path]],
) -> Dict[str, Any]:
    path = media_policy.safe_local_video_path(path, settings)
    stat = path.stat()
    metadata = metadata_loader(path, settings)
    youtube_metadata = youtube_metadata_builder(path, settings)
    marker = marker_reader(path)
    in_uploaded_folder = media_policy.is_path_inside(path, uploaded_root)

    sidecars = set(sidecar_loader(path))
    description_path = path.with_suffix(".youtube-beschreibung.txt")
    metadata_path = path.with_suffix(".youtube.json")
    description_exists = description_path in sidecars
    metadata_exists = metadata_path in sidecars
    prepared = metadata_exists or description_exists
    dashboard_uploaded = str(path) in uploaded_set
    manually_uploaded = bool(marker.get("uploaded"))
    uploaded = (
        dashboard_uploaded or manually_uploaded or in_uploaded_folder
    )

    if in_uploaded_folder:
        status = "Archived"
    elif manually_uploaded:
        status = "Manually Uploaded"
    elif dashboard_uploaded:
        status = "Uploaded by Dashboard"
    elif prepared:
        status = "Prepared for YouTube"
    else:
        status = "Ready"

    return {
        "path": str(path),
        "name": path.name,
        "folder": str(path.parent),
        "relative_folder": (
            str(path.parent.relative_to(download_root))
            if media_policy.is_path_inside(path.parent, download_root)
            else str(path.parent)
        ),
        "size_gb": round(stat.st_size / (1024**3), 2),
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "streamer": metadata.get("streamer") or "",
        "date_de": metadata.get("date_de") or "",
        "title": metadata.get("title") or path.stem,
        "vod_id": metadata.get("vod_id") or "",
        "youtube_title": youtube_metadata.get("title") or path.stem,
        "youtube_description": (
            youtube_metadata.get("description") or ""
        ),
        "description_file": str(description_path),
        "description_file_exists": description_exists,
        "metadata_file": str(metadata_path),
        "metadata_file_exists": metadata_exists,
        "marker_file": str(marker_path_builder(path)),
        "prepared": prepared,
        "dashboard_uploaded": dashboard_uploaded,
        "manually_uploaded": manually_uploaded,
        "already_uploaded": uploaded,
        "in_uploaded_folder": in_uploaded_folder,
        "status": status,
        "uploaded_at": marker.get("uploaded_at") or "",
    }


def enumerate_local_vods(
    settings: Mapping[str, Any],
    include_uploaded: bool,
    *,
    media_policy: MediaPathPolicy,
    uploaded_folder_fallback: Path,
    app_dir: Path,
    payload_builder: Callable[
        [Path, Mapping[str, Any], Set[str]], Dict[str, Any]
    ],
    log_callback: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    root = media_policy.download_path(settings)
    uploaded_root = media_policy.uploaded_vods_folder(
        settings, uploaded_folder_fallback
    )
    uploaded = set(
        map(str, settings.get("youtube_uploaded_files") or [])
    )
    items: List[Dict[str, Any]] = []

    if root.exists():
        for discovered_path in root.rglob("*"):
            if (
                not discovered_path.is_file()
                or discovered_path.suffix.lower() not in VIDEO_EXTENSIONS
            ):
                continue
            path = discovered_path
            try:
                path = media_policy.safe_local_video_path(
                    discovered_path, settings
                )
                if media_policy.is_path_inside(path, app_dir):
                    continue
                if (
                    not include_uploaded
                    and media_policy.is_path_inside(path, uploaded_root)
                ):
                    continue
                items.append(payload_builder(path, settings, uploaded))
            except Exception as exc:
                if log_callback:
                    log_callback(
                        f"Could not read local VOD file {path}: {exc}"
                    )

    items.sort(
        key=lambda item: (
            item.get("already_uploaded", False),
            item.get("mtime", ""),
        ),
        reverse=False,
    )
    pending = [
        item for item in items if not item.get("already_uploaded")
    ]
    marked = [
        item
        for item in items
        if item.get("manually_uploaded")
        or item.get("dashboard_uploaded")
    ]
    total_bytes = sum(
        int(item.get("size_bytes") or 0) for item in items
    )

    return {
        "videos": items,
        "root": str(root),
        "uploaded_root": str(uploaded_root),
        "include_uploaded": include_uploaded,
        "counts": {
            "total": len(items),
            "pending": len(pending),
            "uploaded": len(marked),
            "size_gb": round(total_bytes / (1024**3), 2),
        },
    }
