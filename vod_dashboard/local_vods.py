from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Set

from vod_dashboard.media import MediaPathPolicy, is_complete_video_file


def _path_identity(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").casefold()


def _valid_uploaded_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return text


def _history_uploaded_at(
    settings: Mapping[str, Any], path: Path
) -> str:
    identity = _path_identity(path)
    for item in reversed(list(settings.get("youtube_upload_history") or [])):
        if not isinstance(item, Mapping):
            continue
        if _path_identity(item.get("path")) != identity:
            continue
        return _valid_uploaded_at(item.get("uploaded_at"))
    return ""


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

    uploaded_at = _valid_uploaded_at(marker.get("uploaded_at"))
    if not uploaded_at:
        uploaded_at = _history_uploaded_at(settings, path)

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
        "uploaded_at": uploaded_at,
        "local_file_exists": True,
    }


def _missing_uploaded_payload(
    path: Path, uploaded_at: str = ""
) -> Dict[str, Any]:
    return {
        "path": str(path),
        "name": path.name,
        "folder": str(path.parent),
        "relative_folder": "",
        "size_gb": None,
        "size_bytes": None,
        "mtime": "",
        "streamer": path.parent.name,
        "date_de": "",
        "title": path.stem,
        "vod_id": "",
        "youtube_title": path.stem,
        "youtube_description": "",
        "description_file": "",
        "description_file_exists": False,
        "metadata_file": "",
        "metadata_file_exists": False,
        "marker_file": "",
        "prepared": False,
        "dashboard_uploaded": True,
        "manually_uploaded": False,
        "already_uploaded": True,
        "in_uploaded_folder": False,
        "status": "Local file removed",
        "uploaded_at": _valid_uploaded_at(uploaded_at),
        "local_file_exists": False,
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
    unfinished_upload_paths: Set[str] | None = None,
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
    unavailable = set(unfinished_upload_paths or set())

    if root.exists():
        for discovered_path in root.rglob("*"):
            if (
                not discovered_path.is_file()
                or not is_complete_video_file(discovered_path)
            ):
                continue
            path = discovered_path
            try:
                path = media_policy.safe_local_video_path(
                    discovered_path, settings
                )
                if media_policy.is_path_inside(path, app_dir):
                    continue
                if str(path) in unavailable:
                    continue
                if (
                    not include_uploaded
                    and media_policy.is_path_inside(path, uploaded_root)
                ):
                    continue
                payload = payload_builder(path, settings, uploaded)
                if not include_uploaded and payload.get("already_uploaded"):
                    continue
                items.append(payload)
            except Exception as exc:
                if log_callback:
                    log_callback(
                        f"Could not read local VOD file {path}: {exc}"
                    )

    if include_uploaded:
        def archive_identity(path: Path) -> str:
            for base in (uploaded_root, root):
                if media_policy.is_path_inside(path, base):
                    try:
                        return _path_identity(path.relative_to(base))
                    except ValueError:
                        pass
            return _path_identity(path)

        existing_history_ids = {
            archive_identity(Path(str(item.get("path") or "")))
            for item in items
            if item.get("path")
        }
        missing_history_ids: Set[str] = set()
        for raw in reversed(list(settings.get("youtube_uploaded_files") or [])):
            try:
                path = media_policy.safe_local_video_path(
                    raw, settings, must_exist=False
                )
                history_id = archive_identity(path)
                if (
                    path.exists()
                    or history_id in existing_history_ids
                    or history_id in missing_history_ids
                ):
                    continue
                items.append(
                    _missing_uploaded_payload(
                        path, _history_uploaded_at(settings, path)
                    )
                )
                missing_history_ids.add(history_id)
            except Exception as exc:
                if log_callback:
                    log_callback(
                        f"Could not read uploaded VOD history entry {raw}: {exc}"
                    )

    pending = [
        item for item in items if not item.get("already_uploaded")
    ]
    marked = [item for item in items if item.get("already_uploaded")]
    pending.sort(key=lambda item: str(item.get("mtime") or ""))
    timestamped = [item for item in marked if item.get("uploaded_at")]
    legacy = [item for item in marked if not item.get("uploaded_at")]
    timestamped.sort(
        key=lambda item: datetime.fromisoformat(
            str(item["uploaded_at"]).replace("Z", "+00:00")
        ).timestamp(),
        reverse=True,
    )
    history_order = {
        _path_identity(raw): index
        for index, raw in enumerate(
            list(settings.get("youtube_uploaded_files") or [])
        )
    }
    legacy.sort(
        key=lambda item: history_order.get(
            _path_identity(item.get("path")), -1
        ),
        reverse=True,
    )
    items = pending + timestamped + legacy
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
