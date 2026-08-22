from __future__ import annotations

import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from vod_dashboard import dashboard_state
from vod_dashboard import local_vods as dashboard_local_vods
from vod_dashboard import jobs as dashboard_jobs
from vod_dashboard import media as dashboard_media
from vod_dashboard import runtime as dashboard_runtime
from vod_dashboard import security as dashboard_security
from vod_dashboard import settings as dashboard_settings
from vod_dashboard import twitch as dashboard_twitch
from vod_dashboard import vod_search as dashboard_vod_search
from vod_dashboard import youtube as dashboard_youtube
from vod_dashboard.twitch import (
    canonical_twitch_vod_url,
    entry_date,
    extract_twitch_vod_id,
    in_range,
    is_live_or_upcoming_entry,
    is_real_vod_url,
    normalize_single_vod_url,
    normalize_vod_url,
    parse_date,
    validate_single_vod_url,
    vod_id_from_url,
)


Credentials = dashboard_youtube.Credentials
InstalledAppFlow = dashboard_youtube.InstalledAppFlow
GoogleRequest = dashboard_youtube.GoogleRequest
google_build = dashboard_youtube.google_build
MediaFileUpload = dashboard_youtube.MediaFileUpload
GOOGLE_LIBS_AVAILABLE = dashboard_youtube.GOOGLE_LIBS_AVAILABLE
YOUTUBE_SCOPES = dashboard_youtube.YOUTUBE_SCOPES
YouTubeNotConnectedError = dashboard_youtube.YouTubeNotConnectedError
YouTubeOAuthBootstrapRequiredError = (
    dashboard_youtube.YouTubeOAuthBootstrapRequiredError
)
VIDEO_EXTENSIONS = dashboard_media.VIDEO_EXTENSIONS

RUNTIME_PATHS = dashboard_runtime.RuntimePaths.from_environment(
    Path(__file__).resolve().parent
)
APP_DIR = RUNTIME_PATHS.app_dir
STREAMER_FILE_NAME = dashboard_runtime.STREAMER_FILE_NAME
ARCHIVE_FILE_NAME = dashboard_runtime.ARCHIVE_FILE_NAME
UPLOADED_VODS_FOLDER_NAME = dashboard_runtime.UPLOADED_VODS_FOLDER_NAME
YTDLP_DEFAULT_OUTPUT_TEMPLATE = dashboard_settings.YTDLP_DEFAULT_OUTPUT_TEMPLATE
MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = (
    dashboard_settings.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
)

# Ab v22 liegen Einstellungen dauerhaft außerhalb des App-Ordners.
# Dadurch gehen sie beim Entpacken/Aktualisieren neuer Dashboard-Versionen nicht mehr verloren.
USER_HOME = RUNTIME_PATHS.user_home
DEFAULT_MEDIA_ROOT = RUNTIME_PATHS.default_media_root
MEDIA_ROOT = RUNTIME_PATHS.media_root
DEFAULT_DASHBOARD_DIR = RUNTIME_PATHS.dashboard_dir
FIXED_STREAMER_FILE = RUNTIME_PATHS.streamer_file
FIXED_ARCHIVE_FILE = RUNTIME_PATHS.archive_file
FIXED_YOUTUBE_CLIENT_SECRET_FILE = RUNTIME_PATHS.youtube_client_secret_file
FIXED_YOUTUBE_TOKEN_FILE = RUNTIME_PATHS.youtube_token_file
FIXED_UPLOADED_VODS_FOLDER = RUNTIME_PATHS.uploaded_vods_folder
LOCAL_SETTINGS_FILE = RUNTIME_PATHS.local_settings_file
SETTINGS_FILE = RUNTIME_PATHS.settings_file
LOG_FILE = RUNTIME_PATHS.log_file
LOG_MAX_BYTES = dashboard_runtime.LOG_MAX_BYTES

DEFAULT_SETTINGS = dashboard_settings.DEFAULT_SETTINGS


canonical_origin = dashboard_security.canonical_origin
SecurityConfig = dashboard_security.SecurityConfig


def security_config_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    return dashboard_security.security_config_from_environment(
        environ, origin_normalizer=canonical_origin
    )


app = Flask(__name__)
app.config.update(security_config_from_environment())
if app.config["VOD_AUTH_DISABLED"]:
    app.logger.critical(
        "SECURITY WARNING: Authentication is disabled by VOD_DASHBOARD_AUTH_DISABLED=1. "
        "Use this mode for local development only."
    )
JOB_MANAGER = dashboard_jobs.JobManager()
job_lock = JOB_MANAGER.lock
jobs = JOB_MANAGER.jobs
job_counter = JOB_MANAGER.counter
log_file_lock = dashboard_runtime.log_file_lock
LOGIN_THROTTLE = dashboard_security.LoginThrottle()
login_attempt_lock = LOGIN_THROTTLE.lock
login_attempts = LOGIN_THROTTLE.attempts
LOGIN_ATTEMPT_WINDOW_SECONDS = dashboard_security.LOGIN_ATTEMPT_WINDOW_SECONDS
LOGIN_MAX_FAILURES = dashboard_security.LOGIN_MAX_FAILURES


def log_line(text: str) -> None:
    dashboard_runtime.log_line(text, LOG_FILE, LOG_MAX_BYTES)


def get_or_create_csrf_token() -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = dashboard_security.generate_csrf_token(secrets.token_urlsafe)
        session["csrf_token"] = token
    return token


def request_has_valid_csrf_token() -> bool:
    expected = str(session.get("csrf_token") or "")
    supplied = str(
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or ""
    )
    return dashboard_security.csrf_token_matches(
        expected, supplied, comparator=secrets.compare_digest
    )


def request_origin_is_allowed() -> bool:
    raw_origin = request.headers.get("Origin")
    raw_referer = request.headers.get("Referer")
    return dashboard_security.origin_is_allowed(
        raw_origin,
        raw_referer,
        app.config.get("VOD_ALLOWED_ORIGINS"),
        request.host_url,
        origin_normalizer=canonical_origin,
    )


def request_host_is_allowed() -> bool:
    return dashboard_security.host_is_allowed(
        request.host, app.config.get("VOD_TRUSTED_HOSTS")
    )


def security_error(message: str, status: int):
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), status
    return message, status


def login_attempt_key() -> str:
    # Do not trust X-Forwarded-For implicitly; proxy-aware client IP handling must be configured later.
    return dashboard_security.login_attempt_key(request.remote_addr)


def _login_throttle_for_compatibility() -> dashboard_security.LoginThrottle:
    return dashboard_security.LoginThrottle(
        attempts=login_attempts,
        lock=login_attempt_lock,
        window_seconds=LOGIN_ATTEMPT_WINDOW_SECONDS,
        max_failures=LOGIN_MAX_FAILURES,
        clock=time.monotonic,
    )


def login_retry_after(key: str) -> int:
    return _login_throttle_for_compatibility().retry_after(key)


def record_login_failure(key: str) -> None:
    _login_throttle_for_compatibility().record_failure(key)


def clear_login_failures(key: str) -> None:
    _login_throttle_for_compatibility().clear_failures(key)


def username_matches(candidate: str) -> bool:
    expected = str(app.config.get("VOD_USERNAME") or "")
    return dashboard_security.username_matches(candidate, expected)


def password_matches(candidate: str) -> bool:
    password_hash = str(app.config.get("VOD_PASSWORD_HASH") or "")
    return dashboard_security.password_matches(password_hash, candidate)


@app.before_request
def enforce_dashboard_security():
    if not request_host_is_allowed():
        return security_error("Invalid Host header.", 400)

    is_mutation = dashboard_security.method_requires_csrf(request.method)
    if request.endpoint == "static":
        return None

    if request.endpoint == "login":
        if is_mutation:
            if not request_origin_is_allowed():
                return security_error("Invalid request origin.", 403)
            if not request_has_valid_csrf_token():
                return security_error("CSRF validation failed.", 403)
        return None

    auth_disabled = bool(app.config.get("VOD_AUTH_DISABLED"))
    authenticated = bool(session.get("authenticated"))
    if not auth_disabled and not authenticated:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required."}), 401
        return redirect(url_for("login"))

    get_or_create_csrf_token()
    if is_mutation:
        if not request_origin_is_allowed():
            return security_error("Invalid request origin.", 403)
        if not request_has_valid_csrf_token():
            return security_error("CSRF validation failed.", 403)
    return None


to_int = dashboard_settings.to_int
to_bool = dashboard_settings.to_bool


def resolve_media_path(
    raw: Any,
    *,
    must_exist: bool = False,
    require_file: bool = False,
    allowed_extensions: Optional[set[str]] = None,
) -> Path:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).resolve_media_path(
        raw,
        must_exist=must_exist,
        require_file=require_file,
        allowed_extensions=allowed_extensions,
    )


def normalize_media_directory(raw: Any, fallback: Path) -> Path:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).normalize_media_directory(
        raw, fallback
    )


def legacy_settings_candidates() -> List[Path]:
    return dashboard_settings.legacy_settings_candidates(SETTINGS_FILE, os.environ)


def read_json_file(path: Path) -> Dict[str, Any]:
    return dashboard_settings.read_json_file(path, log=log_line)


def clean_stale_packaged_paths(settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_settings.clean_stale_packaged_paths(
        settings,
        DEFAULT_DASHBOARD_DIR,
        STREAMER_FILE_NAME,
        ARCHIVE_FILE_NAME,
    )



def fix_template_confusion(settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_settings.fix_template_confusion(
        settings,
        YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
    )



def force_user_data_paths(settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_settings.force_user_data_paths(
        settings,
        dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        FIXED_STREAMER_FILE,
        FIXED_ARCHIVE_FILE,
        FIXED_UPLOADED_VODS_FOLDER,
    )

def normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_settings.normalize_settings(
        settings,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        default_dashboard_dir=DEFAULT_DASHBOARD_DIR,
        fixed_streamer_file=FIXED_STREAMER_FILE,
        fixed_archive_file=FIXED_ARCHIVE_FILE,
        fixed_uploaded_vods_folder=FIXED_UPLOADED_VODS_FOLDER,
        environ=os.environ,
        streamer_file_name=STREAMER_FILE_NAME,
        archive_file_name=ARCHIVE_FILE_NAME,
        uploaded_vods_folder_name=UPLOADED_VODS_FOLDER_NAME,
        ytdlp_default_output_template=YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        manual_upload_default_filename_template=(
            MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
        ),
    )


def _settings_repository() -> dashboard_settings.SettingsRepository:
    return dashboard_settings.SettingsRepository(
        settings_file=SETTINGS_FILE,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        default_settings=DEFAULT_SETTINGS,
        default_dashboard_dir=DEFAULT_DASHBOARD_DIR,
        fixed_streamer_file=FIXED_STREAMER_FILE,
        fixed_archive_file=FIXED_ARCHIVE_FILE,
        fixed_uploaded_vods_folder=FIXED_UPLOADED_VODS_FOLDER,
        environ=os.environ,
        log=log_line,
        ensure_files=ensure_files,
        streamer_file_name=STREAMER_FILE_NAME,
        archive_file_name=ARCHIVE_FILE_NAME,
        uploaded_vods_folder_name=UPLOADED_VODS_FOLDER_NAME,
        ytdlp_default_output_template=YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        manual_upload_default_filename_template=(
            MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
        ),
    )


def load_settings() -> Dict[str, Any]:
    return _settings_repository().load()


def save_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    return _settings_repository().save(data)


def download_path(settings: Optional[Dict[str, Any]] = None) -> Path:
    s = settings or load_settings()
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).download_path(s)


def base_path(settings: Optional[Dict[str, Any]] = None) -> Path:
    # Kompatibilitätsalias für alte Codestellen
    return download_path(settings)




def _runtime_data_repository() -> dashboard_settings.RuntimeDataRepository:
    return dashboard_settings.RuntimeDataRepository(
        app_dir=APP_DIR,
        default_dashboard_dir=DEFAULT_DASHBOARD_DIR,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        fixed_streamer_file=FIXED_STREAMER_FILE,
        fixed_archive_file=FIXED_ARCHIVE_FILE,
        fixed_uploaded_vods_folder=FIXED_UPLOADED_VODS_FOLDER,
        settings_loader=load_settings,
    )


def streamer_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    return _runtime_data_repository().streamer_file(settings)




def legacy_streamer_candidates(settings: Optional[Dict[str, Any]] = None) -> List[Path]:
    return _runtime_data_repository().legacy_streamer_candidates(settings)


clean_streamer_names = dashboard_settings.clean_streamer_names



read_streamers_from_path = dashboard_settings.read_streamers_from_path




write_streamers_to_path = dashboard_settings.write_streamers_to_path






def archive_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    return _runtime_data_repository().archive_file(settings)



def archive_ids(settings: Optional[Dict[str, Any]] = None) -> set[str]:
    return _runtime_data_repository().archive_ids(settings)





def ensure_files(settings: Optional[Dict[str, Any]] = None) -> None:
    _runtime_data_repository().ensure_files(settings)






def read_streamers(settings: Optional[Dict[str, Any]] = None) -> List[str]:
    return _runtime_data_repository().read_streamers(settings)





def write_streamers(names: List[str], settings: Optional[Dict[str, Any]] = None) -> List[str]:
    return _runtime_data_repository().write_streamers(names, settings)




def ytdlp_base_command() -> List[str]:
    return dashboard_twitch.ytdlp_base_command(sys.executable)


ytdlp_cookie_args = dashboard_twitch.ytdlp_cookie_args


def clean_batch_postprocess_mode(value: Any) -> str:
    return dashboard_settings.clean_batch_postprocess_mode(value)



clean_twitch_rate_limit = dashboard_twitch.clean_twitch_rate_limit


def uploaded_vods_folder(settings: Optional[Dict[str, Any]] = None) -> Path:
    s = settings or load_settings()
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).uploaded_vods_folder(
        s, FIXED_UPLOADED_VODS_FOLDER
    )


def move_uploaded_vod_to_done_folder(path: Path, settings: Dict[str, Any], job_id: Optional[str] = None) -> Path:
    """Verschiebt erfolgreich hochgeladene Dateien wirklich und prüft, dass die Quelle weg ist."""
    return dashboard_youtube.move_uploaded_vod_to_done_folder(
        path,
        settings,
        job_id,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        move_bundle=move_video_bundle_verified,
        job_log_callback=append_job_log,
    )



def build_download_command(urls: List[str], settings: Dict[str, Any]) -> tuple[List[str], Path]:
    return dashboard_twitch.build_download_command(
        urls,
        settings,
        download_directory=download_path(settings),
        archive_path=archive_file(settings),
        command_factory=ytdlp_base_command,
        cookie_args_factory=ytdlp_cookie_args,
    )




def youtube_client_secret_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    return resolve_youtube_client_secret_file(settings)




def youtube_token_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    return resolve_youtube_token_file(settings)



def youtube_available() -> bool:
    return dashboard_youtube.youtube_available(GOOGLE_LIBS_AVAILABLE)



def get_youtube_credentials(settings: Optional[Dict[str, Any]] = None, interactive: bool = False):
    resolved = settings or load_settings()
    return dashboard_youtube.get_youtube_credentials(
        resolved,
        interactive,
        token_path=youtube_token_file(resolved),
        secret_path=youtube_client_secret_file(resolved),
        libraries_available=GOOGLE_LIBS_AVAILABLE,
        interactive_oauth_allowed=(
            dashboard_youtube.youtube_interactive_oauth_enabled()
        ),
        credentials_class=Credentials,
        request_factory=GoogleRequest,
        flow_class=InstalledAppFlow,
        settings_loader=load_settings,
        settings_saver=save_settings,
        log_callback=log_line,
    )



def get_youtube_service(settings: Optional[Dict[str, Any]] = None, interactive: bool = False):
    resolved = settings or load_settings()
    return dashboard_youtube.get_youtube_service(
        resolved,
        interactive,
        credentials_getter=get_youtube_credentials,
        build_factory=google_build,
    )



youtube_path_is_stale = dashboard_youtube.youtube_path_is_stale


def youtube_client_secret_candidates(settings: Optional[Dict[str, Any]] = None) -> List[Path]:
    s = settings or load_settings()
    return dashboard_youtube.youtube_client_secret_candidates(
        s,
        fixed_client_secret_file=FIXED_YOUTUBE_CLIENT_SECRET_FILE,
        default_dashboard_dir=DEFAULT_DASHBOARD_DIR,
        app_dir=APP_DIR,
    )


def resolve_youtube_client_secret_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    s = settings or load_settings()
    return dashboard_youtube.resolve_youtube_client_secret_file(
        s,
        fixed_client_secret_file=FIXED_YOUTUBE_CLIENT_SECRET_FILE,
        default_dashboard_dir=DEFAULT_DASHBOARD_DIR,
        app_dir=APP_DIR,
        candidates_provider=youtube_client_secret_candidates,
    )


def resolve_youtube_token_file(settings: Optional[Dict[str, Any]] = None) -> Path:
    s = settings or load_settings()
    return dashboard_youtube.resolve_youtube_token_file(
        s,
        fixed_token_file=FIXED_YOUTUBE_TOKEN_FILE,
    )


def youtube_connect_error_payload(exc: Exception, settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_youtube.youtube_connect_error_payload(
        exc,
        settings,
        secret_path=resolve_youtube_client_secret_file(settings),
        token_path=resolve_youtube_token_file(settings),
        libraries_available=GOOGLE_LIBS_AVAILABLE,
    )



def youtube_status(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resolved = settings or load_settings()
    return dashboard_youtube.youtube_status(
        resolved,
        secret_path=youtube_client_secret_file(resolved),
        token_path=youtube_token_file(resolved),
        secret_candidates=youtube_client_secret_candidates(resolved),
        libraries_available=GOOGLE_LIBS_AVAILABLE,
        service_getter=get_youtube_service,
    )



def list_youtube_playlists(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    resolved = settings or load_settings()
    return dashboard_youtube.list_youtube_playlists(
        resolved,
        service_getter=get_youtube_service,
    )



format_duration = dashboard_youtube.format_duration
safe_filename_title = dashboard_youtube.safe_filename_title
apply_youtube_template = dashboard_youtube.apply_youtube_template
sanitize_windows_filename = dashboard_youtube.sanitize_windows_filename
guess_video_title = dashboard_youtube.guess_video_title


def parse_info_json(path: Path) -> Dict[str, Any]:
    return dashboard_youtube.parse_info_json(
        path,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        log_callback=log_line,
    )


def metadata_from_path(path: Path, settings: Dict[str, Any]) -> Dict[str, str]:
    return dashboard_youtube.metadata_from_path(
        path,
        settings,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        entry_date_parser=entry_date,
        date_parser=parse_date,
        info_loader=parse_info_json,
        title_builder=safe_filename_title,
        duration_formatter=format_duration,
        log_callback=log_line,
    )


def build_youtube_metadata(path: Path, settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_youtube.build_youtube_metadata(
        path,
        settings,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        entry_date_parser=entry_date,
        date_parser=parse_date,
        info_loader=parse_info_json,
        metadata_loader=metadata_from_path,
        template_renderer=apply_youtube_template,
        title_builder=safe_filename_title,
        log_callback=log_line,
    )


def manual_upload_filename(path: Path, settings: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    return dashboard_youtube.manual_upload_filename(
        path,
        settings,
        metadata,
        template_renderer=apply_youtube_template,
        title_builder=safe_filename_title,
        filename_sanitizer=sanitize_windows_filename,
    )


def unique_path(path: Path) -> Path:
    return dashboard_media.unique_path(path)


def prepare_file_for_manual_youtube_upload(path: Path, settings: Dict[str, Any], job_id: Optional[str] = None) -> Path:
    return dashboard_youtube.prepare_file_for_manual_youtube_upload(
        path,
        settings,
        job_id,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        metadata_builder=build_youtube_metadata,
        filename_builder=manual_upload_filename,
        title_builder=safe_filename_title,
        collision_resolver=unique_path,
        job_log_callback=append_job_log,
    )


def youtube_chunk_mb(settings: Dict[str, Any]) -> int:
    return dashboard_youtube.youtube_chunk_mb(
        settings, int_parser=to_int
    )


def youtube_mode_label(settings: Dict[str, Any]) -> str:
    return dashboard_youtube.youtube_mode_label(
        settings, chunk_size_getter=youtube_chunk_mb
    )


def upload_video_to_youtube(
    path: Path,
    settings: Dict[str, Any],
    job_id: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Optional[str]:
    def update_progress(uploaded: int, total: int) -> None:
        if job_id is not None:
            _job_manager_for_compatibility().update_active_upload_progress(
                job_id, uploaded, total, item_id=item_id
            )

    def cancel_requested() -> bool:
        return bool(
            job_id is not None
            and item_id is not None
            and _job_manager_for_compatibility().is_cancel_requested(
                job_id, item_id
            )
        )

    return dashboard_youtube.upload_video_to_youtube(
        path,
        settings,
        job_id,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        service_getter=get_youtube_service,
        metadata_builder=build_youtube_metadata,
        media_upload_factory=MediaFileUpload,
        chunk_size_getter=youtube_chunk_mb,
        mode_label_getter=youtube_mode_label,
        history_recorder=remember_youtube_uploaded_file,
        move_after_upload=move_uploaded_vod_to_done_folder,
        job_log_callback=append_job_log,
        progress_callback=update_progress if job_id is not None else None,
        cancel_requested=cancel_requested if item_id is not None else None,
    )


def snapshot_video_files(settings: Dict[str, Any]) -> Dict[str, float]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).snapshot_video_files(settings)


def new_video_files(before: Dict[str, float], after: Dict[str, float]) -> List[Path]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).new_video_files(before, after)

def recently_changed_video_files(settings: Dict[str, Any], started_at: float, minutes_buffer: int = 180) -> List[Path]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).recently_changed_video_files(
        settings, started_at, minutes_buffer
    )


def remember_youtube_uploaded_file(path: Path) -> None:
    """Merkt lokal, welche Datei schon über das Dashboard hochgeladen wurde.
    So lädt der Fallback nicht bei jedem späteren Job dieselbe Datei nochmal hoch.
    """
    dashboard_youtube.remember_youtube_uploaded_file(
        path,
        settings_loader=load_settings,
        settings_file=SETTINGS_FILE,
        log_callback=log_line,
    )

def disk_status(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).disk_status(settings)


def _job_manager_for_compatibility() -> dashboard_jobs.JobManager:
    """Use current app globals so tests and legacy callers may still patch them."""
    return dashboard_jobs.JobManager.compatible_with(
        JOB_MANAGER, jobs, job_lock, job_counter
    )


def _set_job_counter(value: int) -> None:
    global job_counter
    job_counter = value


def create_job(urls: List[str], label: str) -> str:
    manager = _job_manager_for_compatibility()
    job_id = manager.create_download_job(
        urls,
        label,
        counter_getter=lambda: job_counter,
        counter_setter=_set_job_counter,
    )
    manager.start_worker(run_download_job, job_id)
    return job_id


def append_job_log(job_id: str, text: str) -> None:
    _job_manager_for_compatibility().append_job_log(
        job_id,
        text,
        log_callback=log_line,
    )


def run_download_job(job_id: str) -> None:
    dependencies = dashboard_jobs.DownloadWorkerDependencies(
        load_settings=load_settings,
        clean_postprocess_mode=clean_batch_postprocess_mode,
        clean_rate_limit=clean_twitch_rate_limit,
        append_log=append_job_log,
        snapshot_video_files=snapshot_video_files,
        new_video_files=new_video_files,
        recently_changed_video_files=recently_changed_video_files,
        prepare_manual_upload=prepare_file_for_manual_youtube_upload,
        get_youtube_service=get_youtube_service,
        upload_to_youtube=upload_video_to_youtube,
        build_download_command=build_download_command,
        download_directory=download_path,
        popen=subprocess.Popen,
        clock=time.time,
        enqueue_upload_job=lambda paths, label: create_upload_job(paths, label),
    )
    dashboard_jobs.run_download_job(
        job_id, _job_manager_for_compatibility(), dependencies
    )



def create_upload_job(
    paths: List[str],
    label: str = "Local YouTube Upload",
    *,
    playlist_id: Optional[str] = None,
) -> str:
    settings = load_settings()
    resolved_playlist_id = str(
        (
            settings.get("youtube_playlist_id")
            if playlist_id is None
            else playlist_id
        )
        or ""
    ).strip()
    manager = _job_manager_for_compatibility()
    unfinished = manager.unfinished_upload_paths()
    uploaded = set(map(str, settings.get("youtube_uploaded_files") or []))
    clean_paths = []
    item_metadata = []
    for raw in paths:
        p = safe_local_video_path(raw, settings)
        if str(p) in clean_paths:
            continue
        if str(p) in unfinished:
            raise RuntimeError("This VOD is already queued for upload.")
        payload = local_video_metadata_payload(p, settings, uploaded)
        if payload.get("already_uploaded"):
            raise RuntimeError("This VOD is already in uploaded history.")
        clean_paths.append(str(p))
        item_metadata.append({
            "streamer": payload.get("streamer") or "",
            "date": payload.get("date_de") or "",
            "title": payload.get("title") or payload.get("youtube_title") or p.name,
            "vod_id": payload.get("vod_id") or "",
            "name": p.name,
            "size_bytes": payload.get("size_bytes"),
            "size_gb": payload.get("size_gb"),
        })
    if not clean_paths:
        raise RuntimeError("No valid VOD files were provided for upload.")
    job_id = manager.create_upload_job(
        clean_paths,
        label,
        playlist_id=resolved_playlist_id,
        item_metadata=item_metadata,
        counter_getter=lambda: job_counter,
        counter_setter=_set_job_counter,
    )
    manager.start_worker(run_upload_job, job_id)
    return job_id


def run_upload_job(job_id: str) -> None:
    dependencies = dashboard_jobs.UploadWorkerDependencies(
        load_settings=load_settings,
        append_log=append_job_log,
        get_youtube_service=get_youtube_service,
        safe_local_video_path=safe_local_video_path,
        upload_to_youtube=upload_video_to_youtube,
    )
    dashboard_jobs.run_upload_job(
        job_id, _job_manager_for_compatibility(), dependencies
    )



def is_path_inside(path: Path, root: Path) -> bool:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).is_path_inside(path, root)


def safe_local_video_path(raw: Any, settings: Dict[str, Any], must_exist: bool = True) -> Path:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).safe_local_video_path(
        raw, settings, must_exist
    )


def local_video_marker_path(path: Path) -> Path:
    return dashboard_media.local_video_marker_path(path)


def local_video_sidecars(path: Path) -> List[Path]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).local_video_sidecars(path)


def read_local_upload_marker(path: Path) -> Dict[str, Any]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).read_local_upload_marker(
        path, log=log_line
    )


def write_local_upload_marker(path: Path, method: str = "manual") -> Dict[str, Any]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).write_local_upload_marker(
        path, method
    )


def local_video_metadata_payload(path: Path, settings: Dict[str, Any], uploaded_set: set[str]) -> Dict[str, Any]:
    return dashboard_local_vods.local_video_metadata_payload(
        path,
        settings,
        uploaded_set,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        download_root=download_path(settings),
        uploaded_root=uploaded_vods_folder(settings),
        metadata_loader=metadata_from_path,
        youtube_metadata_builder=build_youtube_metadata,
        marker_reader=read_local_upload_marker,
        marker_path_builder=local_video_marker_path,
        sidecar_loader=local_video_sidecars,
    )


def enumerate_local_vods(
    settings: Dict[str, Any], include_uploaded: bool
) -> Dict[str, Any]:
    return dashboard_local_vods.enumerate_local_vods(
        settings,
        include_uploaded,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        uploaded_folder_fallback=FIXED_UPLOADED_VODS_FOLDER,
        app_dir=APP_DIR,
        payload_builder=local_video_metadata_payload,
        unfinished_upload_paths=(
            _job_manager_for_compatibility().unfinished_upload_paths()
        ),
        log_callback=log_line,
    )


def move_video_bundle_verified(path: Path, settings: Dict[str, Any], job_id: Optional[str] = None) -> Dict[str, Any]:
    job_log = None
    if job_id:
        job_log = lambda text: append_job_log(job_id, text)
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).move_video_bundle_verified(
        path,
        settings,
        FIXED_UPLOADED_VODS_FOLDER,
        log=log_line,
        job_log=job_log,
    )


def delete_video_bundle_permanently(path: Path, settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_media.MediaPathPolicy(MEDIA_ROOT).delete_video_bundle_permanently(
        path, settings
    )



@app.get("/api/local-videos")
def api_local_videos():
    settings = load_settings()
    include_uploaded = to_bool(request.args.get("include_uploaded"), False)
    return jsonify(enumerate_local_vods(settings, include_uploaded))


def is_windows_platform() -> bool:
    return os.name == "nt"


@app.post("/api/local-video/open")
def api_local_video_open():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    mode = str(data.get("mode") or "select")

    try:
        if mode == "description":
            target = resolve_media_path(
                path.with_suffix(".youtube-beschreibung.txt"),
                must_exist=True,
                require_file=True,
            )
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif mode == "folder":
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
        else:
            if is_windows_platform():
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                os.startfile(str(path.parent))  # type: ignore[attr-defined]
        return jsonify({"ok": True, "path": str(path), "mode": mode})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/local-video/mark-uploaded")
def api_local_video_mark_uploaded():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    marker = write_local_upload_marker(path, method="manual")
    remember_youtube_uploaded_file(path)
    return jsonify({"ok": True, "path": str(path), "marker": marker})


@app.post("/api/local-video/move-uploaded")
def api_local_video_move_uploaded():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    if not read_local_upload_marker(path) and not to_bool(data.get("force"), False):
        return jsonify({
            "ok": False,
            "error": "The VOD is not marked as manually uploaded. Complete the upload, then mark it as uploaded.",
        }), 400
    result = move_video_bundle_verified(path, settings)
    return jsonify(result)


@app.post("/api/local-video/delete")
def api_local_video_delete():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    confirm_name = str(data.get("confirm_name") or "")
    if confirm_name != path.name:
        return jsonify({"ok": False, "error": "The confirmation does not match the filename."}), 400
    result = delete_video_bundle_permanently(path, settings)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status



@app.post("/api/youtube/upload-local")
def api_youtube_upload_local():
    data = request.json or {}
    paths = data.get("paths") or []
    playlist_id = None
    if "playlist_id" in data:
        playlist_id = str(data.get("playlist_id") or "").strip()
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(p).strip() for p in paths if str(p).strip()]
    if not paths:
        return jsonify({"error": "No files selected."}), 400
    try:
        if playlist_id is None:
            job_id = create_upload_job(paths)
        else:
            job_id = create_upload_job(paths, playlist_id=playlist_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"job_id": job_id})



@app.post("/api/manual-upload/prepare-local")
def api_prepare_local_for_manual_upload():
    data = request.json or {}
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    settings = load_settings()
    changed = []
    errors = []
    for raw in paths:
        try:
            p = safe_local_video_path(raw, settings)
            before = str(p)
            new_path = prepare_file_for_manual_youtube_upload(p, settings, job_id=None)
            changed.append({"old": before, "new": str(new_path)})
        except Exception as exc:
            errors.append({"path": str(raw), "error": str(exc)})
    return jsonify({"prepared": changed, "errors": errors})


@app.errorhandler(Exception)
def handle_any_error(exc):
    log_line(f"SERVER ERROR: {type(exc).__name__}: {exc}")
    if request.path.startswith('/api/'):
        safe_message = "Internal server error."
        raw_message = str(exc)
        if raw_message.startswith(
            "The path is outside the administrator-configured media root:"
        ):
            safe_message = (
                "The path is outside the administrator-configured media root."
            )
        elif raw_message in {
            "No media path provided.",
            "Unsupported VOD file type.",
            "No valid VOD files were provided for upload.",
        }:
            safe_message = raw_message
        return jsonify({"error": f"{type(exc).__name__}: {safe_message}"}), 500
    error_type = escape(type(exc).__name__)
    return (
        "<h1>Twitch VOD Dashboard - Error</h1>"
        f"<p><b>{error_type}</b>: An unexpected server error occurred.</p>"
        "<p>Additional details are available in <code>dashboard.log</code>.</p>"
        "<p>Common causes include an inaccessible data folder or invalid settings file.</p>",
        500,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if app.config.get("VOD_AUTH_DISABLED") or session.get("authenticated"):
        return redirect(url_for("index"))

    csrf_token = get_or_create_csrf_token()
    error = ""
    if request.method == "POST":
        username = str(request.form.get("username") or "")
        password = str(request.form.get("password") or "")
        attempt_key = login_attempt_key()
        retry_after = login_retry_after(attempt_key)
        if retry_after:
            return (
                render_template(
                    "login.html",
                    csrf_token=csrf_token,
                    error="Too many login attempts. Try again later.",
                ),
                429,
                {"Retry-After": str(retry_after)},
            )

        password_ok = password_matches(password)
        if username_matches(username) and password_ok:
            clear_login_failures(attempt_key)
            session.clear()
            session["authenticated"] = True
            session["username"] = str(app.config.get("VOD_USERNAME") or "")
            session["csrf_token"] = dashboard_security.generate_csrf_token(
                secrets.token_urlsafe
            )
            session.permanent = True
            return redirect(url_for("index"))

        record_login_failure(attempt_key)
        app.logger.warning("Failed login from %s", request.remote_addr or "unknown")
        error = "Incorrect username or password."
        return render_template("login.html", csrf_token=csrf_token, error=error), 401

    return render_template("login.html", csrf_token=csrf_token, error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({
        "authenticated": bool(session.get("authenticated")) or bool(app.config.get("VOD_AUTH_DISABLED")),
        "auth_disabled": bool(app.config.get("VOD_AUTH_DISABLED")),
        "username": str(session.get("username") or ""),
        "csrf_token": get_or_create_csrf_token(),
    })


@app.route("/")
def index():
    return render_template(
        "index.html",
        csrf_token=get_or_create_csrf_token(),
        auth_disabled=bool(app.config.get("VOD_AUTH_DISABLED")),
    )


@app.get("/api/dashboard")
def api_dashboard():
    settings = load_settings()
    all_jobs = _job_manager_for_compatibility().snapshot_jobs()
    return jsonify(dashboard_state.dashboard_status_payload(
        all_jobs,
        youtube_status(settings),
        disk_status(settings),
        youtube_mode_label(settings),
        youtube_chunk_mb(settings),
    ))


@app.get("/api/state")
def state():
    settings = load_settings()
    ids = archive_ids(settings)
    resolved_streamer_file = streamer_file(settings)
    return jsonify(dashboard_state.application_state_payload(
        settings,
        str(SETTINGS_FILE),
        str(LOCAL_SETTINGS_FILE),
        SETTINGS_FILE.exists(),
        read_streamers_from_path(resolved_streamer_file),
        len(ids),
        download_path_exists=download_path(settings).exists(),
        streamer_file_exists=resolved_streamer_file.exists(),
        streamer_file_resolved=str(resolved_streamer_file),
        streamer_file_forced=FIXED_STREAMER_FILE,
        archive_file_exists=archive_file(settings).exists(),
        archive_file_resolved=str(archive_file(settings)),
        archive_file_forced=FIXED_ARCHIVE_FILE,
    ))


@app.get("/api/settings/status")
def api_settings_status():
    settings = load_settings()
    info = dashboard_state.settings_status_payload(
        str(SETTINGS_FILE),
        SETTINGS_FILE.exists(),
        SETTINGS_FILE.parent.exists(),
        str(LOCAL_SETTINGS_FILE),
        legacy_settings_candidates(),
        str(download_path(settings)),
        str(FIXED_STREAMER_FILE),
        str(FIXED_ARCHIVE_FILE),
    )
    info["can_write_settings_folder"] = dashboard_state.directory_is_writable(
        SETTINGS_FILE.parent
    )
    return jsonify(info)



@app.post("/api/settings")
def api_settings():
    saved = save_settings(request.json or {})
    saved["_settings_file"] = str(SETTINGS_FILE)
    saved["_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(saved)



@app.post("/api/streamers/repair-newlines")
def api_streamers_repair_newlines():
    settings = load_settings()
    path = streamer_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    before_raw = ""
    if path.exists():
        try:
            before_raw = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            before_raw = path.read_text(encoding="latin-1")

    streamers = read_streamers_from_path(path)
    write_streamers_to_path(path, streamers)

    after_raw = path.read_text(encoding="utf-8")
    return jsonify({
        "ok": True,
        "streamer_file": str(path),
        "count": len(streamers),
        "streamers": streamers,
        "had_literal_newlines": "\\n" in before_raw or "\\r\\n" in before_raw,
        "before_length": len(before_raw),
        "after_length": len(after_raw),
    })


@app.get("/api/streamers/status")
def api_streamers_status():
    settings = load_settings()
    path = streamer_file(settings)
    streamers = read_streamers_from_path(path)
    raw = (
        path.read_text(encoding="utf-8-sig", errors="ignore")
        if path.exists()
        else ""
    )
    info = dashboard_state.streamer_status_payload(
        str(FIXED_STREAMER_FILE),
        exists=path.exists(),
        parent_exists=path.parent.exists(),
        streamers=streamers,
        legacy_candidates=legacy_streamer_candidates(settings),
        raw_preview=raw[:300],
        has_literal_newlines=("\\n" in raw or "\\r\\n" in raw),
    )
    info["can_write"] = dashboard_state.directory_is_writable(path.parent)
    return jsonify(info)



@app.post("/api/streamers/force-fixed-path")
def api_streamers_force_fixed_path():
    settings = load_settings()
    settings["download_path"] = str(DEFAULT_DASHBOARD_DIR)
    settings["streamer_file"] = str(FIXED_STREAMER_FILE)
    settings["archive_file"] = str(FIXED_ARCHIVE_FILE)
    saved = save_settings(settings)
    ensure_files(saved)
    streamers = read_streamers(saved)
    return jsonify({
        "ok": True,
        "download_path": str(DEFAULT_DASHBOARD_DIR),
        "streamer_file": str(streamer_file(saved)),
        "archive_file": str(archive_file(saved)),
        "count": len(streamers),
        "streamers": streamers,
    })


@app.post("/api/streamers")
def api_streamers():
    data = request.json or {}
    names = data.get("streamers", [])
    if isinstance(names, str):
        names = names.splitlines()
    settings = load_settings()
    streamers = write_streamers(names, settings)
    return jsonify({"streamers": streamers, "streamer_file": str(FIXED_STREAMER_FILE), "count": len(streamers)})




def run_ytdlp_vod_detail(url: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_twitch.run_ytdlp_vod_detail(
        url,
        settings,
        command_factory=ytdlp_base_command,
        cookie_args_factory=ytdlp_cookie_args,
    )




def run_ytdlp_json_sources(streamer: str, limit: Any = None, settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return dashboard_twitch.run_ytdlp_json_sources(
        streamer,
        limit,
        settings,
        settings_loader=load_settings,
        command_factory=ytdlp_base_command,
        cookie_args_factory=ytdlp_cookie_args,
    )




def run_ytdlp_json_for_streamer(streamer: str, settings: Dict[str, Any], limit: int) -> Dict[str, Any]:
    return dashboard_twitch.run_ytdlp_json_for_streamer(
        streamer,
        settings,
        limit,
        source_runner=run_ytdlp_json_sources,
    )





@app.post("/api/search")
def api_search():
    data = request.json or {}
    settings = load_settings()
    payload = dashboard_vod_search.search_vods_from_payload(
        data,
        settings,
        read_streamers(settings),
        archive_ids(settings),
        date_parser=parse_date,
        integer_parser=to_int,
        search_service=dashboard_twitch.search_vods,
        source_runner=run_ytdlp_json_sources,
        detail_runner=run_ytdlp_vod_detail,
        log_callback=log_line,
    )
    return jsonify(payload)



@app.post("/api/vod/validate")
def api_vod_validate():
    data = request.json or {}
    return jsonify(validate_single_vod_url(data.get("url") or data.get("vod_url") or ""))


@app.post("/api/download")
def api_download():
    data = request.json or {}
    selection = dashboard_vod_search.prepare_download_selection(
        data, validator=validate_single_vod_url
    )
    if selection.error is not None:
        return jsonify(selection.error), 400

    job_id = create_job(selection.urls, selection.label)
    return jsonify({"ok": True, "job_id": job_id, "urls": selection.urls, "url_count": len(selection.urls), "label": selection.label})


@app.get("/api/jobs")
def api_jobs():
    manager = _job_manager_for_compatibility()
    jobs_snapshot = manager.snapshot_jobs(reverse=True)
    return jsonify({
        "jobs": jobs_snapshot,
        "queue_controls": manager.queue_controls_snapshot(),
        "persistence": "process-local",
    })


def _queue_action_identity() -> tuple[str, str]:
    data = request.json or {}
    return (
        str(data.get("job_id") or "").strip(),
        str(data.get("item_id") or "").strip(),
    )


@app.post("/api/queue/pause")
def api_pause_queue():
    lane = str((request.json or {}).get("lane") or "").strip()
    if not _job_manager_for_compatibility().pause_queue(lane):
        return jsonify({"error": "A valid Queue lane is required."}), 400
    return jsonify({"ok": True, "lane": lane, "queue_paused": True})


@app.post("/api/queue/resume")
def api_resume_queue():
    lane = str((request.json or {}).get("lane") or "").strip()
    if not _job_manager_for_compatibility().resume_queue(lane):
        return jsonify({"error": "A valid Queue lane is required."}), 400
    return jsonify({"ok": True, "lane": lane, "queue_paused": False})


@app.post("/api/jobs/stop-after-current")
def api_stop_after_current():
    job_id, item_id = _queue_action_identity()
    if not job_id or not item_id:
        return jsonify({"error": "A valid Queue item is required."}), 400
    if not _job_manager_for_compatibility().request_stop_after_current(
        job_id, item_id
    ):
        return jsonify({"error": "Only the current running item can stop its Queue."}), 409
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "item_id": item_id,
        "stop_after_current": True,
    })


@app.post("/api/jobs/remove-item")
def api_remove_queue_item():
    job_id, item_id = _queue_action_identity()
    if not job_id or not item_id:
        return jsonify({"error": "A valid Queue item is required."}), 400
    if not _job_manager_for_compatibility().remove_queued_item(job_id, item_id):
        return jsonify({"error": "Only a waiting item can be removed from the Queue."}), 409
    return jsonify({"ok": True, "job_id": job_id, "item_id": item_id, "state": "cancelled"})


@app.post("/api/jobs/cancel-item")
def api_cancel_queue_item():
    job_id, item_id = _queue_action_identity()
    if not job_id or not item_id:
        return jsonify({"error": "A valid Queue item is required."}), 400
    manager = _job_manager_for_compatibility()
    already_requested = manager.is_cancel_requested(job_id, item_id)
    lane = manager.request_cancel_item(job_id, item_id)
    if lane is None:
        return jsonify({"error": "Only the current running item can be cancelled."}), 409
    if lane == "download" and not already_requested:
        def terminate_owned_process() -> None:
            try:
                manager.terminate_registered_download(job_id, item_id)
            except Exception as exc:
                append_job_log(job_id, f"Download cancellation error: {exc}")

        threading.Thread(target=terminate_owned_process, daemon=True).start()
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "item_id": item_id,
        "state": manager.item_state(job_id, item_id) or "cancelling",
    })


@app.post("/api/jobs/retry-item")
def api_retry_queue_item():
    job_id, item_id = _queue_action_identity()
    if not job_id or not item_id:
        return jsonify({"error": "A valid Queue item is required."}), 400
    manager = _job_manager_for_compatibility()
    retry = manager.reserve_retry(job_id, item_id)
    if retry is None:
        return jsonify({"error": "Only a failed Queue item can be retried."}), 409
    if retry.get("blocked"):
        return jsonify({"error": retry.get("reason"), "outcome_uncertain": True}), 409
    if not retry.get("reserved"):
        return jsonify({
            "ok": True,
            "duplicate": True,
            "pending": bool(retry.get("pending")),
            "retry_job_id": retry.get("retry_job_id") or "",
        })
    try:
        if retry.get("type") == "youtube_upload":
            retry_job_id = create_upload_job(
                [str(retry["value"])], "Retry YouTube Upload"
            )
        else:
            retry_job_id = create_job(
                [str(retry["value"])], "Retry Twitch Download"
            )
    except RuntimeError as exc:
        manager.finalize_retry(job_id, item_id, "")
        return jsonify({"error": str(exc)}), 409
    except Exception:
        manager.finalize_retry(job_id, item_id, "")
        raise
    manager.finalize_retry(job_id, item_id, retry_job_id)
    manager.update_job(
        retry_job_id,
        retry_of={"job_id": job_id, "item_id": item_id},
    )
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "item_id": item_id,
        "retry_job_id": retry_job_id,
        "fresh_attempt": True,
    })


@app.post("/api/jobs/resolve-error")
def api_resolve_job_error():
    data = request.json or {}
    job_id = str(data.get("job_id") or "").strip()
    item_id = str(data.get("item_id") or "").strip()
    if not job_id or not item_id:
        return jsonify({"error": "A valid Queue item is required."}), 400
    resolved = _job_manager_for_compatibility().resolve_error_by_id(
        job_id, item_id
    )
    if not resolved:
        return jsonify({"error": "Only an unresolved failed item can be resolved."}), 409
    return jsonify({"ok": True, "job_id": job_id, "item_id": item_id})



@app.get("/api/youtube/status")
def api_youtube_status():
    settings = load_settings()
    return jsonify(youtube_status(settings))


@app.post("/api/youtube/connect")
def api_youtube_connect():
    settings = load_settings()
    try:
        service = get_youtube_service(settings, interactive=True)
        resp = service.channels().list(part="snippet", mine=True).execute()
        title = ""
        if resp.get("items"):
            title = resp["items"][0].get("snippet", {}).get("title", "")
        return jsonify({"ok": True, "channel_title": title, "status": youtube_status(settings)})
    except Exception as exc:
        payload = youtube_connect_error_payload(exc, settings)
        log_line(f"YouTube connection error: {payload}")
        # Bewusst kein 500 mehr: Frontend soll den echten Grund anzeigen.
        return jsonify(payload), 400



@app.get("/api/youtube/playlists")
def api_youtube_playlists():
    settings = load_settings()
    try:
        playlists = list_youtube_playlists(settings)
    except YouTubeNotConnectedError:
        playlists = []
    return jsonify({"playlists": playlists})


@app.post("/api/youtube/upload-file")
def api_youtube_upload_file():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    video_id = upload_video_to_youtube(path, settings, job_id=None)
    return jsonify({"ok": True, "video_id": video_id})



@app.post("/api/youtube/preview-file")
def api_youtube_preview_file():
    data = request.json or {}
    settings = load_settings()
    path = safe_local_video_path(data.get("path"), settings)
    return jsonify(build_youtube_metadata(path, settings))

@app.post("/api/open-folder")
def api_open_folder():
    settings = load_settings()
    path = download_path(settings)
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    ensure_files()
    url = "http://127.0.0.1:8787"
    if os.environ.get("VOD_DASHBOARD_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8787, debug=False)
