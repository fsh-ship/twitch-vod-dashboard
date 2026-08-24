from __future__ import annotations

import json
import mimetypes
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from vod_dashboard.media import MediaPathPolicy, unique_path
from vod_dashboard.runtime_files import atomic_write_text


try:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as google_build
    from googleapiclient.http import MediaFileUpload

    GOOGLE_LIBS_AVAILABLE = True
except Exception:
    Credentials = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    GoogleRequest = None  # type: ignore
    google_build = None  # type: ignore
    MediaFileUpload = None  # type: ignore
    GOOGLE_LIBS_AVAILABLE = False


YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
YOUTUBE_OAUTH_MODE_NATIVE = "native"
YOUTUBE_OAUTH_MODE_EXTERNAL = "external"


class YouTubeNotConnectedError(RuntimeError):
    """Raised when an operation requires an authenticated channel."""


class YouTubeOAuthBootstrapRequiredError(RuntimeError):
    """Raised when interactive OAuth must be completed outside the app."""


class YouTubeUploadOutcomeUncertain(RuntimeError):
    """The server may have accepted bytes or finalized before contact was lost."""

    upload_outcome_uncertain = True


def youtube_available(libraries_available: Optional[bool] = None) -> bool:
    if libraries_available is None:
        libraries_available = GOOGLE_LIBS_AVAILABLE
    return bool(libraries_available)


def youtube_oauth_mode(
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    env = os.environ if environ is None else environ
    mode = str(
        env.get("VOD_DASHBOARD_YOUTUBE_OAUTH_MODE")
        or YOUTUBE_OAUTH_MODE_NATIVE
    ).strip().lower()
    if mode not in {YOUTUBE_OAUTH_MODE_NATIVE, YOUTUBE_OAUTH_MODE_EXTERNAL}:
        raise RuntimeError(
            "VOD_DASHBOARD_YOUTUBE_OAUTH_MODE must be 'native' or 'external'."
        )
    return mode


def youtube_interactive_oauth_enabled(
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    return youtube_oauth_mode(environ) == YOUTUBE_OAUTH_MODE_NATIVE


def youtube_path_is_stale(raw: str) -> bool:
    value = str(raw or "")
    return (
        not value.strip()
        or value.startswith("/mnt/data")
        or value.startswith("/home/oai")
        or "\\mnt\\data" in value
        or "twitch-vod-dashboard" in value.lower()
    )


def youtube_client_secret_candidates(
    settings: Dict[str, Any],
    *,
    fixed_client_secret_file: Path,
    default_dashboard_dir: Path,
    app_dir: Path,
) -> List[Path]:
    candidates: List[Path] = []

    def add(path: Path) -> None:
        try:
            expanded = path.expanduser()
            if expanded not in candidates:
                candidates.append(expanded)
        except Exception:
            pass

    raw = str(settings.get("youtube_client_secret_file") or "").strip()
    if raw:
        add(Path(raw))
    add(fixed_client_secret_file)
    add(default_dashboard_dir / "client_secret.json")
    del app_dir

    return candidates


def resolve_youtube_client_secret_file(
    settings: Dict[str, Any],
    *,
    fixed_client_secret_file: Path,
    default_dashboard_dir: Path,
    app_dir: Path,
    candidates_provider: Optional[
        Callable[[Dict[str, Any]], List[Path]]
    ] = None,
) -> Path:
    raw = str(settings.get("youtube_client_secret_file") or "").strip()

    if raw and not youtube_path_is_stale(raw):
        path = Path(raw).expanduser()
        if path.exists():
            return path

    candidates = (
        candidates_provider(settings)
        if candidates_provider
        else youtube_client_secret_candidates(
            settings,
            fixed_client_secret_file=fixed_client_secret_file,
            default_dashboard_dir=default_dashboard_dir,
            app_dir=app_dir,
        )
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            pass

    return fixed_client_secret_file.expanduser()


def youtube_client_secret_file(
    settings: Dict[str, Any],
    *,
    fixed_client_secret_file: Path,
    default_dashboard_dir: Path,
    app_dir: Path,
    candidates_provider: Optional[
        Callable[[Dict[str, Any]], List[Path]]
    ] = None,
) -> Path:
    return resolve_youtube_client_secret_file(
        settings,
        fixed_client_secret_file=fixed_client_secret_file,
        default_dashboard_dir=default_dashboard_dir,
        app_dir=app_dir,
        candidates_provider=candidates_provider,
    )


def resolve_youtube_token_file(
    settings: Dict[str, Any], *, fixed_token_file: Path
) -> Path:
    del settings
    return fixed_token_file.expanduser()


def youtube_token_file(
    settings: Dict[str, Any], *, fixed_token_file: Path
) -> Path:
    return resolve_youtube_token_file(
        settings, fixed_token_file=fixed_token_file
    )


def _persist_youtube_token(path: Path, serialized: str) -> None:
    # Reject incomplete serialization before replacing the last known-good token.
    value = json.loads(serialized)
    if not isinstance(value, dict):
        raise ValueError("YouTube token serialization must be a JSON object.")
    atomic_write_text(path, serialized, mode=0o600)


def bootstrap_youtube_oauth(
    client_secret_path: Path,
    token_path: Path,
    *,
    libraries_available: Optional[bool] = None,
    flow_class: Any = None,
) -> Any:
    if not youtube_available(libraries_available):
        raise RuntimeError(
            "Required Google libraries are unavailable. Install the dependencies from requirements.txt."
        )

    secret_path = Path(client_secret_path).expanduser()
    destination = Path(token_path).expanduser()
    if not secret_path.exists():
        raise RuntimeError(f"client_secret.json not found: {secret_path}")

    oauth_flow_class = flow_class or InstalledAppFlow
    flow = oauth_flow_class.from_client_secrets_file(
        str(secret_path), YOUTUBE_SCOPES
    )
    credentials = flow.run_local_server(port=0, prompt="consent")
    _persist_youtube_token(destination, credentials.to_json())
    return credentials


def get_youtube_credentials(
    settings: Dict[str, Any],
    interactive: bool = False,
    *,
    token_path: Path,
    secret_path: Path,
    libraries_available: Optional[bool] = None,
    interactive_oauth_allowed: bool = True,
    credentials_class: Any = None,
    request_factory: Any = None,
    flow_class: Any = None,
    settings_loader: Optional[Callable[[], Dict[str, Any]]] = None,
    settings_saver: Optional[
        Callable[[Dict[str, Any]], Dict[str, Any]]
    ] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Any:
    if not youtube_available(libraries_available):
        raise RuntimeError(
            "Required Google libraries are unavailable. Install the dependencies from requirements.txt."
        )

    credentials_class = credentials_class or Credentials
    request_factory = request_factory or GoogleRequest
    flow_class = flow_class or InstalledAppFlow
    credentials = None

    if token_path.exists():
        try:
            credentials = credentials_class.from_authorized_user_file(
                str(token_path), YOUTUBE_SCOPES
            )
        except Exception as exc:
            if log_callback:
                log_callback(
                    "Could not read the YouTube token; a new token will be created: "
                    + str(exc)
                )
            credentials = None

    if (
        credentials
        and credentials.expired
        and credentials.refresh_token
    ):
        try:
            credentials.refresh(request_factory())
            _persist_youtube_token(token_path, credentials.to_json())
        except Exception as exc:
            if log_callback:
                log_callback(
                    "YouTube token refresh failed; reconnect YouTube: "
                    + str(exc)
                )
            credentials = None

    if (not credentials or not credentials.valid) and interactive:
        if not interactive_oauth_allowed:
            raise YouTubeOAuthBootstrapRequiredError(
                "Interactive YouTube OAuth is disabled in this deployment. "
                "Run the documented external OAuth bootstrap command and restart or refresh the dashboard."
            )

        credentials = bootstrap_youtube_oauth(
            secret_path,
            token_path,
            libraries_available=libraries_available,
            flow_class=flow_class,
        )

        try:
            if not settings_loader or not settings_saver:
                raise RuntimeError("Settings persistence is unavailable.")
            saved = settings_loader()
            saved["youtube_client_secret_file"] = str(secret_path)
            saved["youtube_token_file"] = str(token_path)
            settings_saver(saved)
        except Exception as exc:
            if log_callback:
                log_callback(
                    f"Could not save YouTube paths after OAuth: {exc}"
                )

    if not credentials or not credentials.valid:
        raise YouTubeNotConnectedError(
            "YouTube is not connected. Use Connect YouTube on the YouTube page first."
        )

    return credentials


def get_youtube_service(
    settings: Dict[str, Any],
    interactive: bool = False,
    *,
    credentials_getter: Callable[[Dict[str, Any], bool], Any],
    build_factory: Any = None,
) -> Any:
    credentials = credentials_getter(settings, interactive=interactive)
    builder = build_factory or google_build
    return builder(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def youtube_connect_error_payload(
    exc: Exception,
    settings: Dict[str, Any],
    *,
    secret_path: Path,
    token_path: Path,
    libraries_available: Optional[bool] = None,
) -> Dict[str, Any]:
    del settings
    raw_message = str(exc)
    lowered_message = raw_message.lower()
    if isinstance(exc, YouTubeOAuthBootstrapRequiredError):
        public_detail = raw_message
    elif "redirect_uri_mismatch" in lowered_message:
        public_detail = "redirect_uri_mismatch"
    elif "invalid_client" in lowered_message:
        public_detail = "invalid_client"
    elif "access_denied" in lowered_message:
        public_detail = "access_denied"
    elif not secret_path.exists():
        public_detail = f"client_secret.json not found: {secret_path}"
    else:
        public_detail = "YouTube OAuth connection failed."
    message = f"{type(exc).__name__}: {public_detail}"

    if isinstance(exc, YouTubeOAuthBootstrapRequiredError):
        hint = (
            "Authorize YouTube outside the container with "
            "python -m vod_dashboard.youtube_oauth --client-secret ./data/client_secret.json "
            "--token ./data/youtube-token.json, then refresh this page."
        )
    elif not youtube_available(libraries_available):
        hint = (
            "Required Google libraries are unavailable. Install the dependencies from requirements.txt."
        )
    elif not secret_path.exists():
        hint = (
            f"client_secret.json is missing. Place it at {secret_path} or enter the correct path on the YouTube page."
        )
    elif "redirect_uri_mismatch" in lowered_message:
        hint = (
            "The OAuth client is probably incorrect. In Google Cloud, use an OAuth client of type Desktop app."
        )
    elif "invalid_client" in lowered_message:
        hint = (
            "client_secret.json is invalid, damaged, or belongs to the wrong OAuth client. Download a new Desktop app client secret."
        )
    elif "access_denied" in lowered_message:
        hint = "Google sign-in was cancelled or access was denied."
    else:
        hint = (
            "The token file is created after a successful connection. Check client_secret.json and the error below."
        )

    return {
        "ok": False,
        "error": message,
        "hint": hint,
        "client_secret_path": str(secret_path),
        "client_secret_exists": secret_path.exists(),
        "token_path": str(token_path),
        "token_exists": token_path.exists(),
        "google_libs_available": youtube_available(libraries_available),
    }


def youtube_status(
    settings: Dict[str, Any],
    *,
    secret_path: Path,
    token_path: Path,
    secret_candidates: List[Path],
    libraries_available: Optional[bool] = None,
    service_getter: Optional[Callable[[Dict[str, Any], bool], Any]] = None,
) -> Dict[str, Any]:
    token_exists = token_path.exists()
    secret_exists = secret_path.exists()
    connected = False
    channel_title = ""
    error = ""

    if token_exists and youtube_available(libraries_available):
        try:
            if not service_getter:
                raise RuntimeError("A YouTube service getter is required.")
            service = service_getter(settings, interactive=False)
            response = (
                service.channels()
                .list(part="snippet", mine=True)
                .execute()
            )
            items = response.get("items", [])
            connected = bool(items)
            if items:
                channel_title = items[0].get("snippet", {}).get("title", "")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return {
        "google_libs_available": youtube_available(libraries_available),
        "client_secret_exists": secret_exists,
        "client_secret_path": str(secret_path),
        "client_secret_candidates": [str(path) for path in secret_candidates],
        "token_exists": token_exists,
        "token_path": str(token_path),
        "connected": connected,
        "channel_title": channel_title,
        "error": error,
    }


def list_youtube_playlists(
    settings: Dict[str, Any],
    *,
    service_getter: Callable[[Dict[str, Any], bool], Any],
) -> List[Dict[str, str]]:
    service = service_getter(settings, interactive=False)
    playlists: List[Dict[str, str]] = []
    page_token = None
    while True:
        response = (
            service.playlists()
            .list(
                part="snippet",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("items", []):
            playlists.append(
                {
                    "id": item.get("id", ""),
                    "title": item.get("snippet", {}).get(
                        "title", "Untitled"
                    ),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return playlists


def youtube_chunk_mb(
    settings: Dict[str, Any],
    *,
    int_parser: Optional[Callable[[Any, int], int]] = None,
) -> int:
    parser = int_parser or _to_int
    mode = str(
        settings.get("youtube_upload_mode") or "stable"
    ).strip().lower()
    manual = max(
        1, parser(settings.get("youtube_chunk_size_mb"), 64)
    )
    if mode == "safe":
        return 32
    if mode == "fast":
        return 128
    if mode == "manual":
        return manual
    return 64


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def youtube_mode_label(
    settings: Dict[str, Any],
    *,
    chunk_size_getter: Callable[[Dict[str, Any]], int] = youtube_chunk_mb,
) -> str:
    mode = str(
        settings.get("youtube_upload_mode") or "stable"
    ).strip().lower()
    if mode == "safe":
        return "Very Stable"
    if mode == "fast":
        return "Fast"
    if mode == "manual":
        return f"Manual ({chunk_size_getter(settings)} MB)"
    return "Stable"


def remember_youtube_uploaded_file(
    path: Path,
    *,
    settings_loader: Callable[[], Dict[str, Any]],
    settings_file: Optional[Path] = None,
    settings_saver: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    now: Callable[[], datetime] = datetime.now,
) -> None:
    settings = settings_loader()
    current = list(settings.get("youtube_uploaded_files") or [])
    value = str(path)
    if value not in current:
        current.append(value)
    settings["youtube_uploaded_files"] = current
    history = [
        dict(item)
        for item in settings.get("youtube_upload_history") or []
        if isinstance(item, Mapping)
        and str(item.get("path") or "").strip()
        and str(item.get("uploaded_at") or "").strip()
    ]
    history = [item for item in history if str(item.get("path")) != value]
    history.append(
        {"path": value, "uploaded_at": now().isoformat(timespec="seconds")}
    )
    settings["youtube_upload_history"] = history
    try:
        if settings_saver is not None:
            settings_saver(settings)
        elif settings_file is not None:
            atomic_write_text(
                settings_file, json.dumps(settings, indent=2, ensure_ascii=False)
            )
        else:
            raise RuntimeError("Settings persistence is unavailable.")
    except Exception:
        if log_callback:
            try:
                log_callback("Could not save the YouTube upload history.")
            except Exception:
                pass
        raise


def move_uploaded_vod_to_done_folder(
    path: Path,
    settings: Dict[str, Any],
    job_id: Optional[str] = None,
    *,
    media_policy: MediaPathPolicy,
    move_bundle: Callable[..., Dict[str, Any]],
    job_log_callback: Optional[Callable[[str, str], None]] = None,
) -> Path:
    if not settings.get("move_uploaded_vods", True):
        return path
    path = media_policy.safe_local_video_path(path, settings)
    try:
        result = move_bundle(path, settings, job_id=job_id)
        return Path(result["new_path"])
    except Exception as exc:
        if job_id and job_log_callback:
            job_log_callback(
                job_id, f"Move after upload failed: {exc}"
            )
        return path


def upload_video_to_youtube(
    path: Path,
    settings: Dict[str, Any],
    job_id: Optional[str] = None,
    *,
    media_policy: MediaPathPolicy,
    service_getter: Callable[..., Any],
    metadata_builder: Callable[[Path, Dict[str, Any]], Dict[str, Any]],
    media_upload_factory: Callable[..., Any],
    chunk_size_getter: Callable[[Dict[str, Any]], int] = youtube_chunk_mb,
    mode_label_getter: Callable[[Dict[str, Any]], str] = youtube_mode_label,
    history_recorder: Callable[[Path], None],
    move_after_upload: Callable[..., Path],
    job_log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_requested: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    path = media_policy.safe_local_video_path(path, settings)
    service = service_getter(settings, interactive=False)
    privacy = str(
        settings.get("youtube_privacy_status") or "private"
    )
    if privacy not in {"private", "unlisted", "public"}:
        privacy = "private"
    tags_raw = str(settings.get("youtube_tags") or "")
    tags = [tag.strip() for tag in tags_raw.split(",") if tag.strip()]
    youtube_metadata = metadata_builder(path, settings)
    body = {
        "snippet": {
            "title": sanitize_youtube_title(
                youtube_metadata.get("title"),
                fallback=safe_filename_title(path),
            ),
            "description": sanitize_youtube_description(
                youtube_metadata.get("description")
            ),
            "tags": tags,
            "categoryId": str(
                settings.get("youtube_category_id") or "20"
            ),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    mimetype = mimetypes.guess_type(str(path))[0] or "video/mp4"
    chunk_mb = chunk_size_getter(settings)
    media = media_upload_factory(
        str(path),
        mimetype=mimetype,
        chunksize=chunk_mb * 1024 * 1024,
        resumable=True,
    )
    if job_id and job_log_callback:
        job_log_callback(
            job_id,
            f"YouTube Upload starting: {path.name} ({privacy})",
        )
        job_log_callback(
            job_id,
            f"YouTube Title: {body['snippet']['title']}",
        )
        metadata_line = youtube_metadata.get("meta", {})
        job_log_callback(
            job_id,
            "YouTube Metadata: Streamer="
            f"{metadata_line.get('streamer', '') or 'unknown'} · Date="
            f"{metadata_line.get('date_de', '') or 'unknown'} · VOD ID="
            f"{metadata_line.get('vod_id', '') or 'unknown'}",
        )
        job_log_callback(
            job_id,
            f"YouTube Upload Mode: {mode_label_getter(settings)} · "
            f"Chunk Size: {chunk_size_getter(settings)} MB",
        )
    upload_request = service.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    response = None
    while response is None:
        if response is None and cancel_requested and cancel_requested():
            raise RuntimeError("YouTube upload cancelled.")
        try:
            status, response = upload_request.next_chunk()
        except Exception as exc:
            if cancel_requested and cancel_requested():
                raise RuntimeError("YouTube upload cancelled.") from exc
            response_status = getattr(
                getattr(exc, "resp", None), "status", None
            )
            known_rejection = (
                isinstance(response_status, int)
                and 400 <= response_status < 500
                and response_status not in {408, 409, 429}
            )
            if known_rejection:
                raise
            raise YouTubeUploadOutcomeUncertain(
                f"{exc}. YouTube upload status is uncertain because the resumable request ended without a trustworthy final response. Verify the video in YouTube Studio before retrying."
            ) from exc
        if status:
            total_bytes = getattr(status, "total_size", None)
            if not isinstance(total_bytes, int) or total_bytes <= 0:
                total_bytes = path.stat().st_size
            bytes_uploaded = getattr(status, "resumable_progress", None)
            if not isinstance(bytes_uploaded, int):
                bytes_uploaded = int(status.progress() * total_bytes)
            if progress_callback and total_bytes > 0:
                progress_callback(bytes_uploaded, total_bytes)
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    f"YouTube Upload {path.name}: "
                    f"{int(status.progress() * 100)}%",
                )
        if response is None and cancel_requested and cancel_requested():
            raise RuntimeError("YouTube upload cancelled.")
    video_id = response.get("id") if response else None
    if video_id and job_id and job_log_callback:
        job_log_callback(
            job_id,
            "YouTube Upload completed: "
            f"https://www.youtube.com/watch?v={video_id}",
        )
    if video_id:
        history_recorder(path)
    playlist_id = str(
        settings.get("youtube_playlist_id") or ""
    ).strip()
    if video_id and playlist_id:
        try:
            service.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            ).execute()
            if job_id and job_log_callback:
                job_log_callback(
                    job_id, f"Added to playlist: {playlist_id}"
                )
        except Exception as exc:
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    f"Could not add VOD to playlist: {exc}",
                )
    if video_id:
        moved_path = move_after_upload(path, settings, job_id=job_id)
        if moved_path != path:
            history_recorder(moved_path)
    return video_id


def format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except Exception:
        return ""
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def safe_filename_title(path: Path) -> str:
    title = path.stem
    title = re.sub(r"\s+", " ", title).strip()
    return title


def parse_info_json(
    path: Path,
    *,
    media_policy: MediaPathPolicy,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    candidates = [
        path.with_suffix(".info.json"),
        path.with_name(path.stem + ".info.json"),
    ]
    try:
        candidates.extend(sorted(path.parent.glob(path.stem + "*.info.json")))
    except Exception:
        pass
    for candidate in candidates:
        if candidate.exists():
            try:
                candidate = media_policy.resolve_media_path(
                    candidate, must_exist=True, require_file=True
                )
                return json.loads(candidate.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                if log_callback:
                    log_callback(f"Could not read info JSON {candidate}: {exc}")
    return {}


def _is_live_recording_info(info: Dict[str, Any]) -> bool:
    """Identify metadata captured while a source was actively live."""

    live_status = str(info.get("live_status") or "").strip().lower()
    return info.get("is_live") is True or live_status == "is_live"


def metadata_from_path(
    path: Path,
    settings: Dict[str, Any],
    *,
    media_policy: MediaPathPolicy,
    entry_date_parser: Callable[[Dict[str, Any]], Optional[str]],
    date_parser: Callable[[Optional[str]], Optional[datetime]],
    info_loader: Optional[Callable[[Path], Dict[str, Any]]] = None,
    title_builder: Callable[[Path], str] = safe_filename_title,
    duration_formatter: Callable[[Any], str] = format_duration,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    path = media_policy.safe_local_video_path(path, settings)
    info = (
        info_loader(path)
        if info_loader
        else parse_info_json(
            path, media_policy=media_policy, log_callback=log_callback
        )
    )
    filename_title = title_builder(path)
    is_live_recording = _is_live_recording_info(info)
    vod_id = "" if is_live_recording else str(info.get("id") or "")
    if not vod_id and not is_live_recording:
        match = re.search(r"\[(\d{6,})\]", filename_title) or re.search(
            r"videos[\\/](\d{6,})", str(path)
        )
        vod_id = match.group(1) if match else ""
    url = str(info.get("webpage_url") or info.get("original_url") or "")
    if not url and vod_id:
        url = f"https://www.twitch.tv/videos/{vod_id}"
    streamer = str(
        info.get("uploader_id")
        or info.get("uploader")
        or info.get("channel")
        or ""
    )
    if not streamer:
        try:
            streamer = path.parent.name if path.parent.name else ""
        except Exception:
            streamer = ""
    title = str(info.get("title") or filename_title)
    if is_live_recording:
        title = str(info.get("description") or title).strip() or title
    date_raw = entry_date_parser(info)
    if not date_raw:
        match = re.search(
            r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})", filename_title
        )
        if match:
            date_raw = (
                f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            )
    if not date_raw:
        try:
            date_raw = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d"
            )
        except Exception:
            date_raw = datetime.now().strftime("%Y-%m-%d")
    date_de = date_raw
    parsed_date = date_parser(date_raw)
    if parsed_date:
        date_de = parsed_date.strftime("%d.%m.%Y")
    duration = duration_formatter(info.get("duration"))
    return {
        "title": title,
        "streamer": streamer,
        "date": date_raw,
        "date_de": date_de,
        "vod_id": vod_id,
        "url": url,
        "duration": duration,
        "filename": path.name,
        "filepath": str(path),
    }


def apply_youtube_template(
    template: str, meta: Dict[str, str], fallback: str = ""
) -> str:
    if not template:
        return fallback
    values = {key: str(value or "") for key, value in meta.items()}
    try:
        result = template.format(**values)
    except Exception:
        result = fallback or template
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def sanitize_youtube_title(
    value: Any,
    fallback: Any = "YouTube Upload",
    max_len: int = 95,
) -> str:
    """Return a safe final YouTube title without changing source metadata."""

    def clean(candidate: Any) -> str:
        characters = []
        for character in str(candidate or ""):
            if character in "<>":
                characters.append(" ")
            elif unicodedata.category(character).startswith("C"):
                characters.append(" ")
            else:
                characters.append(character)
        return re.sub(r"\s+", " ", "".join(characters)).strip()

    title = clean(value) or clean(fallback) or "YouTube Upload"
    if max_len > 0 and len(title) > max_len:
        title = title[:max_len].rstrip()
    return title or "YouTube Upload"


def sanitize_youtube_description(value: Any) -> str:
    """Return a YouTube-safe description without mutating source metadata.

    Keep ordinary Unicode and line breaks intact, while removing markup-like
    angle brackets and Unicode control/format characters that YouTube rejects.
    """
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    characters = []
    for character in raw:
        if character in "<>":
            continue
        if character == "\n":
            characters.append(character)
        elif character == "\t":
            characters.append(" ")
        elif unicodedata.category(character).startswith("C"):
            continue
        else:
            characters.append(character)
    return "".join(characters)


def build_youtube_metadata(
    path: Path,
    settings: Dict[str, Any],
    *,
    media_policy: MediaPathPolicy,
    entry_date_parser: Callable[[Dict[str, Any]], Optional[str]],
    date_parser: Callable[[Optional[str]], Optional[datetime]],
    info_loader: Optional[Callable[[Path], Dict[str, Any]]] = None,
    metadata_loader: Optional[
        Callable[[Path, Dict[str, Any]], Dict[str, str]]
    ] = None,
    template_renderer: Callable[
        [str, Dict[str, str], str], str
    ] = apply_youtube_template,
    title_builder: Callable[[Path], str] = safe_filename_title,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    meta = (
        metadata_loader(path, settings)
        if metadata_loader
        else metadata_from_path(
            path,
            settings,
            media_policy=media_policy,
            entry_date_parser=entry_date_parser,
            date_parser=date_parser,
            info_loader=info_loader,
            title_builder=title_builder,
            log_callback=log_callback,
        )
    )
    title_template = str(
        settings.get("youtube_title_template")
        or "{streamer} VOD - {date_de} - {title}"
    )
    description_template = str(
        settings.get("youtube_description_template")
        or settings.get("youtube_description")
        or ""
    )
    expanded_title = template_renderer(
        title_template, meta, title_builder(path)
    )
    title = sanitize_youtube_title(
        expanded_title,
        fallback=title_builder(path),
    )
    description = sanitize_youtube_description(template_renderer(
        description_template,
        meta,
        str(settings.get("youtube_description") or ""),
    ))
    return {"title": title, "description": description, "meta": meta}


def sanitize_windows_filename(name: str, max_len: int = 150) -> str:
    raw = str(name or "").strip()
    cleaned_chars = []
    for character in raw:
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        if category in {"So", "Sk"}:
            continue
        if character in '<>:"/\\|?*':
            cleaned_chars.append(" ")
            continue
        cleaned_chars.append(character)

    name = "".join(cleaned_chars)
    name = re.sub(r"(?i)\b!socials\b", "socials", name)
    name = re.sub(r"(?i)\b!cc\b", "cc", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\s*[-–—]+\s*", " - ", name)
    name = re.sub(r"(?:\s*-\s*){2,}", " - ", name)
    name = re.sub(r"\s+([.,])", r"\1", name)
    name = re.sub(r"([.,]){2,}", r"\1", name)
    name = name.strip(" .-_")

    if not name:
        name = "YouTube Upload"

    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if name.upper() in reserved:
        name = "_" + name

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .-_")

    return name or "YouTube Upload"


def manual_upload_filename(
    path: Path,
    settings: Dict[str, Any],
    metadata: Dict[str, Any],
    *,
    template_renderer: Callable[
        [str, Dict[str, str], str], str
    ] = apply_youtube_template,
    title_builder: Callable[[Path], str] = safe_filename_title,
    filename_sanitizer: Callable[[str], str] = sanitize_windows_filename,
) -> str:
    meta = dict(metadata.get("meta") or {})
    template = str(
        settings.get("manual_upload_filename_template")
        or "{date_de} - {streamer} - {title}"
    )
    raw = template_renderer(
        template,
        meta,
        metadata.get("title") or title_builder(path),
    )
    safe = filename_sanitizer(raw)
    if len(safe) < 5:
        safe = filename_sanitizer(title_builder(path))
    return safe


def prepare_file_for_manual_youtube_upload(
    path: Path,
    settings: Dict[str, Any],
    job_id: Optional[str] = None,
    *,
    media_policy: MediaPathPolicy,
    metadata_builder: Callable[[Path, Dict[str, Any]], Dict[str, Any]],
    filename_builder: Callable[
        [Path, Dict[str, Any], Dict[str, Any]], str
    ] = manual_upload_filename,
    title_builder: Callable[[Path], str] = safe_filename_title,
    collision_resolver: Callable[[Path], Path] = unique_path,
    job_log_callback: Optional[Callable[[str, str], None]] = None,
) -> Path:
    path = media_policy.safe_local_video_path(path, settings)
    if not settings.get("manual_upload_prepare_enabled", True):
        return path

    metadata = dict(metadata_builder(path, settings))
    title = sanitize_youtube_title(
        metadata.get("title"),
        fallback=title_builder(path),
    )
    metadata["title"] = title
    description = sanitize_youtube_description(metadata.get("description") or "")
    metadata["description"] = description
    target_path = path

    old_info_candidates = [
        path.with_suffix(".info.json"),
        path.with_name(path.stem + ".info.json"),
    ]

    if settings.get("manual_upload_rename_video", True):
        safe_name = filename_builder(path, settings, metadata)
        desired = media_policy.resolve_media_path(
            path.with_name(safe_name + path.suffix.lower())
        )
        if desired.resolve() != path.resolve():
            desired = collision_resolver(desired)
            try:
                path.rename(desired)
                if job_id and job_log_callback:
                    job_log_callback(
                        job_id,
                        f"Prepare for YouTube: renamed VOD to {desired.name}",
                    )
                for info_path in old_info_candidates:
                    if info_path.exists():
                        new_info = desired.with_suffix(".info.json")
                        if not new_info.exists():
                            try:
                                new_info = media_policy.resolve_media_path(
                                    new_info
                                )
                                info_path = media_policy.resolve_media_path(
                                    info_path,
                                    must_exist=True,
                                    require_file=True,
                                )
                                new_info.write_text(
                                    info_path.read_text(encoding="utf-8-sig"),
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass
                        break
                target_path = desired
            except Exception as exc:
                if job_id and job_log_callback:
                    job_log_callback(
                        job_id,
                        f"Prepare for YouTube: rename failed: {exc}",
                    )
                target_path = path

    if settings.get("manual_upload_write_description", True):
        description_path = media_policy.resolve_media_path(
            target_path.with_suffix(".youtube-beschreibung.txt")
        )
        try:
            description_path.write_text(
                "YouTube Title:\n"
                + title
                + "\n\nYouTube Description:\n"
                + description
                + "\n",
                encoding="utf-8",
            )
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    "Prepare for YouTube: description saved to "
                    + description_path.name,
                )
        except Exception as exc:
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    f"Prepare for YouTube: could not save description: {exc}",
                )

    if settings.get("manual_upload_write_metadata_json", True):
        metadata_path = media_policy.resolve_media_path(
            target_path.with_suffix(".youtube.json")
        )
        try:
            metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    "Prepare for YouTube: metadata saved to "
                    + metadata_path.name,
                )
        except Exception as exc:
            if job_id and job_log_callback:
                job_log_callback(
                    job_id,
                    "Prepare for YouTube: could not save metadata JSON: "
                    + str(exc),
                )

    return target_path


def guess_video_title(path: Path) -> str:
    title = path.stem
    title = re.sub(r"\s+", " ", title).strip()
    return title[:95] if len(title) > 95 else title
