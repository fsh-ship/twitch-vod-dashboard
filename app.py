from __future__ import annotations

import json
import math
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
from typing import Any, Dict, Iterable, List, Mapping, Optional

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from vod_dashboard import dashboard_state
from vod_dashboard import auto_recorder as dashboard_auto_recorder
from vod_dashboard import auto_recording as dashboard_auto_recording
from vod_dashboard import auto_recording_runtime as dashboard_auto_runtime
from vod_dashboard import auto_vod as dashboard_auto_vod
from vod_dashboard import auto_youtube_handoff as dashboard_auto_youtube_handoff
from vod_dashboard import auto_youtube_generate as dashboard_auto_youtube_generate
from vod_dashboard import auto_youtube_materialize as dashboard_auto_youtube_materialize
from vod_dashboard import auto_youtube_plan as dashboard_auto_youtube_plan
from vod_dashboard import auto_youtube_prepare as dashboard_auto_youtube_prepare
from vod_dashboard import auto_youtube_replan as dashboard_auto_youtube_replan
from vod_dashboard import auto_vod_result as dashboard_auto_vod_result
from vod_dashboard import auto_vod_coordinator as dashboard_auto_vod_coordinator
from vod_dashboard import auto_vod_runtime as dashboard_auto_vod_runtime
from vod_dashboard import auto_vod_storage as dashboard_auto_vod_storage
from vod_dashboard import auto_vod_status as dashboard_auto_vod_status
from vod_dashboard import local_vods as dashboard_local_vods
from vod_dashboard import jobs as dashboard_jobs
from vod_dashboard import job_store as dashboard_job_store
from vod_dashboard import live_status as dashboard_live_status
from vod_dashboard import media as dashboard_media
from vod_dashboard import runtime as dashboard_runtime
from vod_dashboard import security as dashboard_security
from vod_dashboard import settings as dashboard_settings
from vod_dashboard import twitch as dashboard_twitch
from vod_dashboard import vod_search as dashboard_vod_search
from vod_dashboard import youtube as dashboard_youtube
from vod_dashboard import youtube_upload_state as dashboard_youtube_upload_state
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
AUTO_RECORDER_MONITOR_LOCK = threading.Lock()
AUTO_RECORDER_MONITOR: Optional[
    dashboard_auto_runtime.AutoRecorderMonitor
] = None
AUTO_VOD_MONITOR_LOCK = threading.Lock()
AUTO_VOD_MONITOR: Optional[dashboard_auto_vod_runtime.AutoVodMonitor] = None
WORKER_RUNTIME_LOCK = threading.RLock()
WORKER_RUNTIME_RESULT: Optional[Dict[str, Any]] = None
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


def build_live_recording_command(
    streamer: str, settings: Dict[str, Any], *, attempt: int = 1
) -> List[str]:
    kwargs: Dict[str, Any] = {
        "download_directory": download_path(settings),
        "command_factory": ytdlp_base_command,
        "cookie_args_factory": ytdlp_cookie_args,
    }
    if attempt != 1:
        kwargs["attempt"] = attempt
    return dashboard_twitch.build_live_recording_command(
        streamer,
        settings,
        **kwargs,
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
        settings_saver=save_settings,
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


def create_auto_recorder_monitor() -> dashboard_auto_runtime.AutoRecorderMonitor:
    """Construct the process-local production monitor without starting it."""
    stop_event = threading.Event()

    def start_unless_stopping(streamer: str, **kwargs: Any) -> str:
        if stop_event.is_set():
            raise RecordingStartError("shutdown_requested", streamer)
        return start_live_recording(streamer, **kwargs)

    coordinator = dashboard_auto_recording.AutoRecorderCoordinator(
        settings_provider=load_settings,
        streamer_provider=lambda settings: read_streamers(dict(settings)),
        live_status_checker=run_ytdlp_live_status,
        state_store=dashboard_auto_recorder.AutoRecorderStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR, log=log_line
        ),
        recording_starter=start_unless_stopping,
        recording_jobs_provider=lambda: (
            _job_manager_for_compatibility().snapshot_jobs()
        ),
        should_stop=stop_event.is_set,
    )
    return dashboard_auto_runtime.AutoRecorderMonitor(
        coordinator,
        stop_event=stop_event,
        log=log_line,
    )


def start_auto_recorder_monitor() -> dashboard_auto_runtime.AutoRecorderMonitor:
    """Idempotently start one monitor in the current Gunicorn worker."""
    global AUTO_RECORDER_MONITOR
    with AUTO_RECORDER_MONITOR_LOCK:
        if AUTO_RECORDER_MONITOR is None:
            AUTO_RECORDER_MONITOR = create_auto_recorder_monitor()
        AUTO_RECORDER_MONITOR.start()
        return AUTO_RECORDER_MONITOR


def create_auto_vod_monitor() -> dashboard_auto_vod_runtime.AutoVodMonitor:
    """Construct the production Auto VOD monitor without starting it."""
    stop_event = threading.Event()
    coordinator = dashboard_auto_vod_coordinator.AutoVodCoordinator(
        settings_provider=load_settings,
        streamer_provider=lambda settings: read_streamers(dict(settings)),
        state_store=dashboard_auto_vod.AutoVodStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        job_manager=JOB_MANAGER,
        archive_ids_provider=lambda settings: archive_ids(dict(settings)),
        worker_target=run_download_job,
        discovery=dashboard_twitch.discover_streamer_vods,
        jobs_provider=lambda: _job_manager_for_compatibility().snapshot_jobs(),
        should_stop=stop_event.is_set,
        storage_provider=lambda settings: dashboard_auto_vod_storage.assess_auto_vod_storage(
            download_path(settings)
        ),
    )
    return dashboard_auto_vod_runtime.AutoVodMonitor(
        coordinator, settings_provider=load_settings, stop_event=stop_event, log=log_line
    )


def start_auto_vod_monitor() -> dashboard_auto_vod_runtime.AutoVodMonitor:
    global AUTO_VOD_MONITOR
    with AUTO_VOD_MONITOR_LOCK:
        if AUTO_VOD_MONITOR is None:
            AUTO_VOD_MONITOR = create_auto_vod_monitor()
        AUTO_VOD_MONITOR.start()
        return AUTO_VOD_MONITOR


def initialize_worker_runtime(*, worker_count: int = 1) -> Dict[str, Any]:
    """Idempotently activate durable production state, then Auto Recorder."""
    global AUTO_RECORDER_MONITOR, AUTO_VOD_MONITOR, WORKER_RUNTIME_RESULT
    try:
        supported_worker_count = int(worker_count) == 1
    except (TypeError, ValueError, OverflowError):
        supported_worker_count = False
    if not supported_worker_count:
        return {
            "initialized": False,
            "usable": False,
            "degraded": True,
            "reason": "unsupported_worker_count",
        }

    with WORKER_RUNTIME_LOCK:
        if WORKER_RUNTIME_RESULT is not None:
            return dict(WORKER_RUNTIME_RESULT)

        manager = JOB_MANAGER
        store_construction_failed = False
        try:
            store = dashboard_job_store.JobStore.from_dashboard_dir(
                DEFAULT_DASHBOARD_DIR
            )
        except Exception:
            store = dashboard_job_store.UnavailableJobStore()
            store_construction_failed = True

        try:
            manager.configure_persistence(store, media_root=MEDIA_ROOT)
            restore = manager.restore_from_store()
        except Exception as exc:
            reason = (
                exc.code
                if isinstance(exc, dashboard_jobs.JobRestoreError)
                else "runtime_initialization_failed"
            )
            WORKER_RUNTIME_RESULT = {
                "initialized": False,
                "usable": False,
                "degraded": True,
                "reason": reason,
            }
            app.logger.error(
                "Worker runtime initialization failed (%s).", reason
            )
            return dict(WORKER_RUNTIME_RESULT)

        auto_youtube_handoff = {"created": 0, "blocked": 0, "pending": 0}
        auto_youtube_plan = {"ready": 0, "attention": 0, "pending": 0}
        auto_youtube_preparation = {
            "ready": 0, "preparing": 0, "blocked": 0,
            "attention": 0, "pending": 0, "ignored": 0,
        }
        auto_youtube_generation = {
            "ready": 0, "blocked": 0, "attention": 0,
            "pending": 0, "ignored": 0,
        }
        auto_youtube_replan = {
            "replanned": 0, "exhausted": 0, "attention": 0,
            "pending": 0, "ignored": 0,
        }
        auto_youtube_materialization = {
            "queued": 0, "attention": 0, "pending": 0, "ignored": 0,
        }
        try:
            auto_youtube_handoff = _auto_youtube_handoff_service(
                manager
            ).reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube handoff reconciliation failed (handoff_reconciliation_failed)."
            )
        try:
            auto_youtube_plan = _auto_youtube_plan_service().reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube plan reconciliation failed (plan_reconciliation_failed)."
            )
        try:
            auto_youtube_preparation = _auto_youtube_preparation_service().reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube preparation failed (preparation_reconciliation_failed)."
            )
        try:
            auto_youtube_generation = _auto_youtube_generation_service().reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube generation failed (generation_reconciliation_failed)."
            )
        try:
            auto_youtube_replan = _auto_youtube_replan_service().reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube replanning failed (replan_reconciliation_failed)."
            )
        try:
            auto_youtube_materialization = _auto_youtube_materialization_service(
                manager
            ).reconcile()
        except Exception:
            app.logger.error(
                "Auto YouTube job materialization failed (materialization_reconciliation_failed)."
            )

        monitor_started = False
        auto_vod_monitor_started = False
        monitor_reason = ""
        try:
            with AUTO_RECORDER_MONITOR_LOCK:
                if AUTO_RECORDER_MONITOR is None:
                    AUTO_RECORDER_MONITOR = create_auto_recorder_monitor()
                monitor = AUTO_RECORDER_MONITOR
            monitor.prepare_after_restart()
            monitor.start()
            monitor_started = True
        except Exception:
            monitor_reason = "monitor_start_failed"
            app.logger.error(
                "Auto recorder monitor startup failed (monitor_start_failed)."
            )

        try:
            with AUTO_VOD_MONITOR_LOCK:
                if AUTO_VOD_MONITOR is None:
                    AUTO_VOD_MONITOR = create_auto_vod_monitor()
                auto_vod_monitor = AUTO_VOD_MONITOR
            auto_vod_monitor.start()
            auto_vod_monitor_started = True
        except Exception:
            monitor_reason = monitor_reason or "auto_vod_monitor_start_failed"
            app.logger.error("Auto VOD monitor startup failed (auto_vod_monitor_start_failed).")

        degraded = bool(
            store_construction_failed or restore.degraded or monitor_reason
        )
        reason = (
            "store_unavailable"
            if store_construction_failed
            else monitor_reason or restore.reason
        )
        WORKER_RUNTIME_RESULT = {
            "initialized": True,
            "usable": True,
            "degraded": degraded,
            "reason": reason,
            "loaded_count": restore.loaded_count,
            "discarded_count": restore.discarded_count,
            "reconciled_job_count": restore.reconciled_job_count,
            "reconciled_item_count": restore.reconciled_item_count,
            "source": restore.source,
            "monitor_started": monitor_started,
            "auto_vod_monitor_started": auto_vod_monitor_started,
            "auto_youtube_handoff": auto_youtube_handoff,
            "auto_youtube_plan": auto_youtube_plan,
            "auto_youtube_preparation": auto_youtube_preparation,
            "auto_youtube_generation": auto_youtube_generation,
            "auto_youtube_replan": auto_youtube_replan,
            "auto_youtube_materialization": auto_youtube_materialization,
        }
        app.logger.info(
            "Worker runtime initialized: loaded=%d discarded=%d "
            "reconciled_jobs=%d reconciled_items=%d degraded=%s source=%s reason=%s.",
            restore.loaded_count,
            restore.discarded_count,
            restore.reconciled_job_count,
            restore.reconciled_item_count,
            degraded,
            restore.source,
            reason or "none",
        )
        return dict(WORKER_RUNTIME_RESULT)


def wake_auto_recorder_monitor() -> bool:
    """Wake the internal monitor after relevant persisted settings change."""
    with AUTO_RECORDER_MONITOR_LOCK:
        monitor = AUTO_RECORDER_MONITOR
    return monitor.wake() if monitor is not None else False


def wake_auto_vod_monitor() -> bool:
    with AUTO_VOD_MONITOR_LOCK:
        monitor = AUTO_VOD_MONITOR
    return monitor.wake() if monitor is not None else False


def _configured_auto_vod_streamers(
    settings: Mapping[str, Any], streamers: Optional[Iterable[Any]] = None
) -> set[str]:
    configured = streamers if streamers is not None else read_streamers(dict(settings))
    profiles = dashboard_settings.normalize_streamer_profiles(settings.get("streamer_profiles"))
    return {
        login for raw_login in configured
        if (login := dashboard_settings.canonical_streamer_login(raw_login))
        and profiles.get(login, {}).get("auto_vod_download") is True
    }


def _wake_auto_vod_after_save(reason: str) -> bool:
    try:
        return wake_auto_vod_monitor()
    except Exception:
        app.logger.warning("Auto VOD monitor wake failed after %s.", reason)
        return False


def _configured_auto_record_streamers(
    settings: Mapping[str, Any], streamers: Optional[Iterable[Any]] = None
) -> set[str]:
    """Return configured logins whose normalized profile opts into recording."""
    configured = streamers if streamers is not None else read_streamers(dict(settings))
    profiles = dashboard_settings.normalize_streamer_profiles(
        settings.get("streamer_profiles")
    )
    return {
        login
        for raw_login in configured
        if (login := dashboard_settings.canonical_streamer_login(raw_login))
        and profiles.get(login, {}).get("auto_record") is True
    }


def _wake_auto_recorder_after_save(reason: str) -> bool:
    """Best-effort wake with bounded logging; persistence has already succeeded."""
    try:
        return wake_auto_recorder_monitor()
    except Exception as exc:
        app.logger.warning(
            "Auto recorder monitor wake failed after %s (%s).",
            reason,
            type(exc).__name__,
        )
        return False


def public_auto_recorder_status(
    settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the strictly allowlisted Auto Recorder status API payload."""
    current_settings = dict(settings or load_settings())
    snapshot = auto_recorder_monitor_snapshot()
    allowed_phases = {
        "checking",
        "degraded",
        "paused",
        "sleeping",
        "starting",
        "stopped",
    }
    allowed_error_codes = {
        "invalid_json",
        "invalid_structure",
        "state_persistence_failed",
        "status_check_failed",
        "thread_start_failed",
        "unexpected_iteration_error",
        "unreadable_state",
        "unsupported_version",
    }
    phase = str(snapshot.get("phase") or "stopped")
    if phase not in allowed_phases:
        phase = "stopped"

    def safe_timestamp(key: str) -> Optional[str]:
        value = snapshot.get(key)
        if value is None:
            return None
        candidate = str(value)
        return candidate[:64] if re.fullmatch(r"[0-9T:.+Z-]{1,64}", candidate) else None

    def bounded_count(key: str) -> int:
        try:
            return max(0, min(10000, int(snapshot.get(key) or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    healthy = snapshot.get("state_healthy")
    if healthy is not True and healthy is not False and healthy is not None:
        healthy = None
    running = snapshot.get("running") is True
    if not running:
        phase = "stopped"
    error_code = dashboard_auto_runtime._safe_code(
        snapshot.get("last_error_code"), ""
    )
    if error_code not in allowed_error_codes:
        error_code = ""
    return {
        "enabled": current_settings.get("auto_recorder_enabled") is True,
        "running": running,
        "state_healthy": healthy,
        "phase": phase,
        "watched_count": len(_configured_auto_record_streamers(current_settings)),
        "last_check_started_at": safe_timestamp("last_check_started_at"),
        "last_check_completed_at": safe_timestamp("last_check_completed_at"),
        "next_check_at": safe_timestamp("next_check_at"),
        "last_action": dashboard_auto_runtime._safe_action(
            snapshot.get("last_action")
        ),
        "last_action_streamer": dashboard_auto_runtime._safe_streamer(
            snapshot.get("last_action_streamer")
        ),
        "error_count_last_run": bounded_count("error_count_last_run"),
        "last_error_code": error_code,
    }


def stop_auto_recorder_monitor(timeout: float = 5.0) -> bool:
    """Idempotently request monitor shutdown and join within a bound."""
    with AUTO_RECORDER_MONITOR_LOCK:
        monitor = AUTO_RECORDER_MONITOR
    return True if monitor is None else monitor.stop(timeout=timeout)


def stop_auto_vod_monitor(timeout: float = 5.0) -> bool:
    with AUTO_VOD_MONITOR_LOCK:
        monitor = AUTO_VOD_MONITOR
    return True if monitor is None else monitor.stop(timeout=timeout)


def auto_vod_monitor_snapshot() -> Dict[str, Any]:
    with AUTO_VOD_MONITOR_LOCK:
        monitor = AUTO_VOD_MONITOR
    return {
        "running": False,
        "thread_alive": False,
        "in_progress": False,
        "last_started_at": None,
        "last_finished_at": None,
        "last_result": None,
        "next_check_at": None,
        "wake_pending": False,
    } if monitor is None else monitor.snapshot()


def public_auto_vod_status(settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    current = dict(settings or load_settings())
    snapshot = auto_vod_monitor_snapshot()
    return dashboard_auto_vod_status.public_auto_vod_status(
        snapshot,
        initialized=AUTO_VOD_MONITOR is not None,
        enabled=current.get("auto_vod_enabled") is True,
        poll_minutes=current.get("auto_vod_poll_minutes"),
        watched_count=len(_configured_auto_vod_streamers(current)),
    )


def auto_recorder_monitor_snapshot() -> Dict[str, Any]:
    """Return internal runtime status without exposing a public endpoint."""
    with AUTO_RECORDER_MONITOR_LOCK:
        monitor = AUTO_RECORDER_MONITOR
    if monitor is None:
        return {
            "running": False,
            "enabled": False,
            "state_healthy": None,
            "watched_count": 0,
            "phase": "stopped",
            "last_check_started_at": None,
            "last_check_completed_at": None,
            "next_check_at": None,
            "last_action": "none",
            "last_action_streamer": "",
            "error_count_last_run": 0,
            "last_error_code": "",
        }
    return monitor.snapshot()


def shutdown_worker_runtime() -> bool:
    """Stop new work, clean up owned processes, then flush durable state."""
    auto_vod_stopped = stop_auto_vod_monitor(timeout=5.0)
    monitor_stopped = stop_auto_recorder_monitor(timeout=5.0)
    manager = _job_manager_for_compatibility()
    manager.begin_shutdown()
    download_result = {"stopped": True}

    def stop_downloads() -> None:
        download_result["stopped"] = manager.stop_downloads_for_shutdown()

    cleanup_deadline = time.monotonic() + 50.0
    download_thread = threading.Thread(target=stop_downloads, daemon=True)
    download_thread.start()
    recording_stopped = manager.stop_recording_for_shutdown(timeout=50.0)
    download_thread.join(
        timeout=max(0.0, cleanup_deadline - time.monotonic())
    )
    downloads_stopped = (
        not download_thread.is_alive() and download_result["stopped"]
    )
    persistence_flushed = manager.flush_persistence()
    if not monitor_stopped:
        log_line("Auto recorder monitor did not stop within its shutdown budget.")
    if not downloads_stopped:
        log_line("Active download did not stop within its shutdown budget.")
    if not recording_stopped:
        log_line("Active recording did not stop within its shutdown budget.")
    if not persistence_flushed:
        log_line("Final job history persistence checkpoint failed.")
    return (
        monitor_stopped
        and downloads_stopped
        and recording_stopped
        and persistence_flushed
    )


def _set_job_counter(value: int) -> None:
    global job_counter
    job_counter = value


def create_job(
    urls: List[str],
    label: str,
    *,
    retry_of: Optional[Dict[str, str]] = None,
) -> str:
    manager = _job_manager_for_compatibility()
    job_id = manager.create_download_job(
        urls,
        label,
        retry_of=retry_of,
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
        resolve_auto_vod_completed_output=resolve_completed_auto_vod_output,
        download_output_marker=dashboard_twitch.DOWNLOAD_FINAL_OUTPUT_MARKER,
        auto_youtube_admission_decision=auto_youtube_admission_decision,
        admit_auto_youtube_intent=admit_auto_youtube_intent,
    )
    dashboard_jobs.run_download_job(
        job_id, _job_manager_for_compatibility(), dependencies
    )


def auto_youtube_admission_decision(
    settings: Mapping[str, Any], streamer: Any
) -> dashboard_auto_youtube_handoff.AutoYouTubeAdmission:
    return dashboard_auto_youtube_handoff.completion_admission(
        settings, streamer
    )


def _auto_youtube_handoff_service(
    manager: Optional[dashboard_jobs.JobManager] = None,
) -> dashboard_auto_youtube_handoff.AutoYouTubeHandoffService:
    return dashboard_auto_youtube_handoff.AutoYouTubeHandoffService(
        job_manager=manager or _job_manager_for_compatibility(),
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
    )


def _auto_youtube_plan_service() -> dashboard_auto_youtube_plan.AutoYouTubePlanService:
    return dashboard_auto_youtube_plan.AutoYouTubePlanService(
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
        metadata_builder=build_youtube_metadata,
    )


def _auto_youtube_preparation_service() -> dashboard_auto_youtube_prepare.AutoYouTubePreparationService:
    return dashboard_auto_youtube_prepare.AutoYouTubePreparationService(
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
    )


def _auto_youtube_generation_service() -> dashboard_auto_youtube_generate.AutoYouTubeGenerationService:
    return dashboard_auto_youtube_generate.AutoYouTubeGenerationService(
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
    )


def _auto_youtube_replan_service() -> dashboard_auto_youtube_replan.AutoYouTubeReplanService:
    return dashboard_auto_youtube_replan.AutoYouTubeReplanService(
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
    )


def _auto_youtube_materialization_service(
    manager: Optional[dashboard_jobs.JobManager] = None,
) -> dashboard_auto_youtube_materialize.AutoYouTubeMaterializationService:
    return dashboard_auto_youtube_materialize.AutoYouTubeMaterializationService(
        state_store=dashboard_youtube_upload_state.YouTubeUploadStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR
        ),
        job_manager=manager or _job_manager_for_compatibility(),
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
    )


def admit_auto_youtube_intent(
    job_id: str, item_id: str, completion_settings: Mapping[str, Any]
) -> str:
    outcome = _auto_youtube_handoff_service().admit_pending(
        job_id,
        item_id,
        plan_inputs=dashboard_auto_youtube_plan.freeze_plan_inputs(
            completion_settings
        ),
    )
    if outcome in {"created", "pending"}:
        _auto_youtube_plan_service().reconcile()
        _auto_youtube_preparation_service().reconcile()
        _auto_youtube_generation_service().reconcile()
        _auto_youtube_replan_service().reconcile()
        _auto_youtube_materialization_service().reconcile()
    return outcome


def resolve_completed_recording_output(
    raw_path: Any, settings: Dict[str, Any]
) -> str:
    policy = dashboard_media.MediaPathPolicy(MEDIA_ROOT)
    path = policy.safe_local_video_path(raw_path, settings, must_exist=True)
    return path.relative_to(policy.media_root).as_posix()


def resolve_completed_auto_vod_output(
    raw_path: Any,
    settings: Dict[str, Any],
    expected_twitch_vod_id: str,
) -> Dict[str, Any]:
    return dashboard_auto_vod_result.resolve_completed_auto_vod_output(
        raw_path,
        settings,
        expected_twitch_vod_id,
        media_policy=dashboard_media.MediaPathPolicy(MEDIA_ROOT),
    )


class RecordingStartError(RuntimeError):
    """A stable internal recording-start failure for HTTP or future callers."""

    def __init__(self, reason: str, streamer: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.streamer = streamer


def create_recording_job(
    streamer: str,
    live_metadata: Mapping[str, Any],
    *,
    settings: Optional[Dict[str, Any]] = None,
    origin: str = "manual",
    attempt: int = 1,
) -> str:
    """Construct and start an already-validated internal recording job."""
    resolved_settings = settings if settings is not None else load_settings()
    manager = _job_manager_for_compatibility()
    job_id = manager.create_recording_job(
        streamer,
        stream_id=str(live_metadata.get("stream_id") or ""),
        title=str(live_metadata.get("title") or ""),
        live_started_at=live_metadata.get("started_at"),
        quality=str(resolved_settings.get("quality") or "source/best"),
        output_name=dashboard_twitch.live_recording_output_template(
            streamer, attempt=attempt
        ),
        origin=origin,
        attempt=attempt,
        counter_getter=lambda: job_counter,
        counter_setter=_set_job_counter,
    )
    manager.start_worker(run_recording_job, job_id)
    return job_id


def start_live_recording(
    streamer: Any,
    *,
    live_metadata: Optional[Mapping[str, Any]] = None,
    origin: str = "manual",
    attempt: int = 1,
) -> str:
    """Validate, reserve, and start one manual or future automatic recording."""
    canonical_login = dashboard_settings.canonical_streamer_login(streamer)
    if not canonical_login:
        raise RecordingStartError("invalid_streamer")

    try:
        normalized_origin, normalized_attempt = (
            dashboard_jobs.validate_recording_job_metadata(origin, attempt)
        )
    except dashboard_jobs.RecordingJobMetadataError as exc:
        raise RecordingStartError(exc.reason, canonical_login) from exc

    settings = load_settings()
    configured_logins = {
        dashboard_settings.canonical_streamer_login(name)
        for name in read_streamers(settings)
    }
    if canonical_login not in configured_logins:
        raise RecordingStartError(
            "streamer_not_configured", canonical_login
        )

    manager = _job_manager_for_compatibility()
    if manager.has_pending_or_active_recording():
        raise RecordingStartError("recording_conflict", canonical_login)

    resolved_live_metadata = live_metadata
    if resolved_live_metadata is None:
        try:
            resolved_live_metadata = run_ytdlp_live_status(
                canonical_login, settings
            )
        except Exception as exc:
            log_line(f"Recording live check failed for {canonical_login}.")
            raise RecordingStartError(
                "live_status_unavailable", canonical_login
            ) from exc
    if not isinstance(resolved_live_metadata, Mapping):
        raise RecordingStartError(
            "live_status_unavailable", canonical_login
        )
    if str(resolved_live_metadata.get("state") or "") != "live":
        raise RecordingStartError("streamer_not_live", canonical_login)

    safe_live_metadata = {
        "stream_id": str(resolved_live_metadata.get("stream_id") or ""),
        "title": str(resolved_live_metadata.get("title") or ""),
        "started_at": (
            str(resolved_live_metadata.get("started_at"))
            if resolved_live_metadata.get("started_at") is not None
            else None
        ),
    }
    try:
        return create_recording_job(
            canonical_login,
            safe_live_metadata,
            settings=settings,
            origin=normalized_origin,
            attempt=normalized_attempt,
        )
    except dashboard_jobs.RecordingConflictError as exc:
        raise RecordingStartError(
            "recording_conflict", canonical_login
        ) from exc
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        raise RecordingStartError(
            exc.code, canonical_login
        ) from exc
    except Exception as exc:
        log_line(f"Recording job creation failed for {canonical_login}.")
        raise RecordingStartError(
            "recording_start_failed", canonical_login
        ) from exc


def run_recording_job(job_id: str) -> None:
    dependencies = dashboard_jobs.RecordingWorkerDependencies(
        load_settings=load_settings,
        append_log=append_job_log,
        build_recording_command=build_live_recording_command,
        download_directory=download_path,
        resolve_completed_output=resolve_completed_recording_output,
        output_marker=dashboard_twitch.LIVE_RECORDING_OUTPUT_MARKER,
        popen=subprocess.Popen,
    )
    dashboard_jobs.run_recording_job(
        job_id, _job_manager_for_compatibility(), dependencies
    )


def persist_manual_recording_stop(job: Mapping[str, Any]) -> bool:
    """Best-effort durable suppression after an accepted recording stop."""
    streamer = dashboard_settings.canonical_streamer_login(
        job.get("streamer")
    )
    stream_id = dashboard_auto_recorder.normalize_auto_recorder_stream_id(
        job.get("stream_id")
    )
    if not streamer or not stream_id:
        return False
    try:
        settings = load_settings()
        configured = {
            dashboard_settings.canonical_streamer_login(value)
            for value in read_streamers(settings)
        }
        profile = dashboard_settings.streamer_profile_for(
            settings, streamer
        )
        if (
            str(job.get("origin") or "") != "auto"
            and not (
                streamer in configured
                and profile.get("auto_record") is True
            )
        ):
            return False
        attempt = job.get("attempt")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 0
        ):
            attempt = 0
        store = dashboard_auto_recorder.AutoRecorderStateStore.from_dashboard_dir(
            DEFAULT_DASHBOARD_DIR, log=log_line
        )
        store.set_handled(
            streamer,
            stream_id,
            "manual_stop",
            attempts=attempt,
            job_id=str(job.get("id") or "") or None,
        )
        return True
    except Exception:
        log_line(
            "Auto recorder manual-stop suppression could not be persisted."
        )
        return False



def create_upload_job(
    paths: List[str],
    label: str = "Local YouTube Upload",
    *,
    playlist_id: Optional[str] = None,
    frozen_item_metadata: Optional[List[Dict[str, Any]]] = None,
    retry_of: Optional[Dict[str, str]] = None,
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
    if frozen_item_metadata is not None and len(frozen_item_metadata) != len(
        paths
    ):
        raise RuntimeError("Frozen upload metadata does not match the files.")
    for source_index, raw in enumerate(paths):
        p = safe_local_video_path(raw, settings)
        if str(p) in clean_paths:
            continue
        if str(p) in unfinished:
            raise RuntimeError("This VOD is already queued for upload.")
        payload = local_video_metadata_payload(p, settings, uploaded)
        if payload.get("already_uploaded"):
            raise RuntimeError("This VOD is already in uploaded history.")
        clean_paths.append(str(p))
        if frozen_item_metadata is not None:
            item_metadata.append(
                dict(frozen_item_metadata[source_index])
            )
        else:
            streamer = payload.get("streamer") or ""
            item_playlist_id = (
                dashboard_settings.resolve_youtube_playlist_for_streamer(
                    settings,
                    streamer,
                    explicit_playlist=playlist_id,
                )
            )
            item_metadata.append({
                "streamer": streamer,
                "date": payload.get("date_de") or "",
                "title": payload.get("title") or payload.get("youtube_title") or p.name,
                "vod_id": dashboard_auto_vod.normalize_auto_vod_id(
                    payload.get("vod_id")
                ),
                "name": p.name,
                "size_bytes": payload.get("size_bytes"),
                "size_gb": payload.get("size_gb"),
                "youtube_playlist_id": item_playlist_id,
            })
    if not clean_paths:
        raise RuntimeError("No valid VOD files were provided for upload.")
    job_id = manager.create_upload_job(
        clean_paths,
        label,
        playlist_id=resolved_playlist_id,
        item_metadata=item_metadata,
        retry_of=retry_of,
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
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        return jsonify({"error": str(exc), "reason": exc.code}), 409
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


@app.get("/api/auto-recorder/status")
def api_auto_recorder_status():
    return jsonify(public_auto_recorder_status())


@app.get("/api/auto-vod/status")
def api_auto_vod_status():
    return jsonify(public_auto_vod_status())


@app.post("/api/auto-vod/check-now")
def api_auto_vod_check_now():
    status = public_auto_vod_status()
    if not status["initialized"] or not status["running"]:
        return jsonify({"ok": False, "status": "unavailable"}), 503
    if not status["enabled"]:
        return jsonify({"ok": False, "status": "disabled"}), 409
    if not status["watched_count"]:
        return jsonify({"ok": False, "status": "no_streamers"}), 409
    return jsonify({"ok": bool(wake_auto_vod_monitor()), "status": "scheduled"})



@app.post("/api/settings")
def api_settings():
    before = load_settings()
    configured_streamers = read_streamers(before)
    before_watched = _configured_auto_record_streamers(
        before, configured_streamers
    )
    before_auto_vod = _configured_auto_vod_streamers(before, configured_streamers)
    saved = save_settings(request.json or {})
    after_watched = _configured_auto_record_streamers(
        saved, configured_streamers
    )
    after_auto_vod = _configured_auto_vod_streamers(saved, configured_streamers)
    if (
        (before.get("auto_recorder_enabled") is True)
        != (saved.get("auto_recorder_enabled") is True)
        or before_watched != after_watched
    ):
        _wake_auto_recorder_after_save("settings save")
    if (
        (before.get("auto_vod_enabled") is True) != (saved.get("auto_vod_enabled") is True)
        or before.get("auto_vod_poll_minutes") != saved.get("auto_vod_poll_minutes")
        or before_auto_vod != after_auto_vod
    ):
        _wake_auto_vod_after_save("settings save")
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
    before_streamers = read_streamers(settings)
    before_watched = _configured_auto_record_streamers(
        settings, before_streamers
    )
    before_auto_vod = _configured_auto_vod_streamers(settings, before_streamers)
    profiles_supplied = "streamer_profiles" in data
    if profiles_supplied:
        settings = save_settings({
            "streamer_profiles": data.get("streamer_profiles")
        })
    streamers = write_streamers(names, settings)
    after_watched = _configured_auto_record_streamers(settings, streamers)
    after_auto_vod = _configured_auto_vod_streamers(settings, streamers)
    if before_watched != after_watched:
        _wake_auto_recorder_after_save("streamer save")
    if before_auto_vod != after_auto_vod or before_streamers != streamers:
        _wake_auto_vod_after_save("streamer save")
    payload = {
        "streamers": streamers,
        "streamer_file": str(FIXED_STREAMER_FILE),
        "count": len(streamers),
    }
    if profiles_supplied:
        payload["streamer_profiles"] = settings.get(
            "streamer_profiles", {}
        )
    return jsonify(payload)




def run_ytdlp_vod_detail(url: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    return dashboard_twitch.run_ytdlp_vod_detail(
        url,
        settings,
        command_factory=ytdlp_base_command,
        cookie_args_factory=ytdlp_cookie_args,
    )




def run_ytdlp_live_status(
    streamer: str, settings: Dict[str, Any]
) -> Dict[str, Any]:
    return dashboard_live_status.LIVE_STATUS_LIMITER.run(
        dashboard_twitch.run_ytdlp_live_status,
        streamer,
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
    if settings.get("auto_vod_enabled") is True:
        try:
            baseline_vod_ids = (
                dashboard_auto_vod.AutoVodStateStore.from_dashboard_dir(
                    DEFAULT_DASHBOARD_DIR
                ).baseline_existing_vod_ids()
            )
        except dashboard_auto_vod.AutoVodStateError:
            log_line("Auto VOD baseline status is unavailable for VOD Search.")
        else:
            configured_streamers = _configured_auto_vod_streamers(settings)
            dashboard_vod_search.apply_auto_vod_baseline_status(
                payload,
                {
                    streamer: vod_ids
                    for streamer, vod_ids in baseline_vod_ids.items()
                    if streamer in configured_streamers
                },
            )
    return jsonify(payload)



@app.get("/api/live/status")
def api_live_status():
    raw_streamer = request.args.get("streamer")
    canonical_login = dashboard_settings.canonical_streamer_login(raw_streamer)
    if not canonical_login:
        return jsonify({"error": "A valid Twitch streamer is required."}), 400

    settings = load_settings()
    configured_logins = {
        dashboard_settings.canonical_streamer_login(streamer)
        for streamer in read_streamers(settings)
    }
    if canonical_login not in configured_logins:
        return jsonify(
            {
                "error": "The Twitch streamer is not configured.",
                "streamer": canonical_login,
            }
        ), 404

    try:
        return jsonify(run_ytdlp_live_status(canonical_login, settings))
    except Exception as exc:
        log_line(f"Live status query failed for {canonical_login}: {exc}")
        return jsonify(
            {
                "error": "The Twitch live status could not be retrieved.",
                "streamer": canonical_login,
            }
        ), 502


def _recording_api_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": str(job.get("id") or ""),
        "state": str(job.get("state") or ""),
        "streamer": str(job.get("streamer") or ""),
        "completion_reason": str(job.get("completion_reason") or ""),
        "output_complete": bool(job.get("output_complete")),
        "output_path": job.get("output_path"),
    }


@app.post("/api/live/record")
def api_start_live_recording():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_request"}), 400
    if set(data) - {"streamer"}:
        return jsonify({"error": "unsupported_recording_parameters"}), 400

    try:
        job_id = start_live_recording(data.get("streamer"))
    except RecordingStartError as exc:
        status_codes = {
            "invalid_streamer": 400,
            "streamer_not_configured": 404,
            "streamer_not_live": 409,
            "recording_conflict": 409,
            "persistence_unavailable": 409,
            "persistence_validation_failed": 409,
            "live_status_unavailable": 502,
            "recording_start_failed": 500,
        }
        public_reason = (
            exc.reason
            if exc.reason in status_codes
            else "recording_start_failed"
        )
        payload = {"error": public_reason}
        if public_reason in {
            "streamer_not_configured",
            "streamer_not_live",
            "live_status_unavailable",
        }:
            payload["streamer"] = exc.streamer
        return jsonify(payload), status_codes[public_reason]

    manager = _job_manager_for_compatibility()
    canonical_login = dashboard_settings.canonical_streamer_login(
        data.get("streamer")
    )
    job = manager.get_job(job_id) or {
        "id": job_id,
        "state": "queued",
        "streamer": canonical_login,
    }
    return jsonify(_recording_api_payload(job)), 201


@app.post("/api/live/record/<job_id>/stop")
def api_stop_live_recording(job_id: str):
    manager = _job_manager_for_compatibility()
    job = manager.get_job(job_id)
    if job is None:
        return jsonify({"error": "recording_job_not_found"}), 404
    if job.get("type") != "recording":
        return jsonify({"error": "not_a_recording_job"}), 409
    if (
        job.get("state") == "completed"
        and job.get("completion_reason") == "stopped_by_user"
    ):
        persist_manual_recording_stop(job)
        return jsonify(_recording_api_payload(job)), 200
    if job.get("state") in {"completed", "failed"}:
        return jsonify(
            {
                "error": "recording_not_stoppable",
                **_recording_api_payload(job),
            }
        ), 409

    if not manager.request_recording_stop(job_id):
        current = manager.get_job(job_id) or job
        if (
            current.get("state") == "completed"
            and current.get("completion_reason") == "stopped_by_user"
        ):
            persist_manual_recording_stop(current)
            return jsonify(_recording_api_payload(current)), 200
        return jsonify(
            {
                "error": "recording_not_stoppable",
                **_recording_api_payload(current),
            }
        ), 409

    current = manager.get_job(job_id) or job
    persist_manual_recording_stop(current)
    manager.start_recording_termination(job_id)
    return jsonify(_recording_api_payload(current)), 202


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

    try:
        job_id = create_job(selection.urls, selection.label)
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        return jsonify({"error": str(exc), "reason": exc.code}), 409
    return jsonify({"ok": True, "job_id": job_id, "urls": selection.urls, "url_count": len(selection.urls), "label": selection.label})


@app.get("/api/jobs")
def api_jobs():
    manager = _job_manager_for_compatibility()
    jobs_snapshot = manager.snapshot_jobs(reverse=True)
    persistence = manager.persistence_status()
    return jsonify({
        "jobs": jobs_snapshot,
        "queue_controls": manager.queue_controls_snapshot(),
        "persistence": "process-local",
        "persistence_status": {
            "enabled": bool(persistence.get("enabled")),
            "healthy": (
                bool(persistence.get("healthy"))
                if persistence.get("healthy") is not None
                else None
            ),
            "current_degraded": bool(
                persistence.get("enabled")
                and persistence.get("healthy") is False
            ),
            "history_degraded": bool(persistence.get("load_degraded")),
        },
    })


@app.post("/api/jobs/clear-completed")
def api_clear_completed_jobs():
    manager = _job_manager_for_compatibility()
    try:
        result = manager.clear_completed_history()
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        return jsonify({
            "error": (
                "Completed history could not be cleared because job history "
                "persistence is unavailable."
            ),
            "reason": exc.code,
        }), 409
    return jsonify({"ok": True, **result})


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


class RetryActionError(RuntimeError):
    """Safe client-facing classification for a rejected retry action."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _canonical_retry_download_url(raw: Any) -> str:
    candidate = str(raw or "").strip()
    canonical = canonical_twitch_vod_url(candidate)
    if (
        not canonical
        or candidate != canonical
        or not re.fullmatch(
            r"https://www\.twitch\.tv/videos/[1-9][0-9]{5,19}",
            canonical,
        )
    ):
        raise RetryActionError(
            "unsafe_source_path",
            "The saved Twitch VOD source is not safe to retry.",
        )
    return canonical


def _validated_retry_upload_path(
    raw: Any, settings: Mapping[str, Any]
) -> Path:
    policy = dashboard_media.MediaPathPolicy(MEDIA_ROOT)
    try:
        candidate = policy.resolve_media_path(raw)
    except RuntimeError as exc:
        raise RetryActionError(
            "unsafe_source_path",
            "The saved upload source is not safe to retry.",
        ) from exc
    if not candidate.exists() or not candidate.is_file():
        raise RetryActionError(
            "source_missing",
            "The upload source file is no longer available.",
        )
    try:
        return policy.safe_local_video_path(
            raw, settings, must_exist=True
        )
    except RuntimeError as exc:
        raise RetryActionError(
            "unsafe_source_path",
            "The saved upload source is not a complete supported video.",
        ) from exc


def _retry_playlist_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if candidate and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", candidate):
        raise RetryActionError(
            "not_retryable",
            "The saved playlist selection is not safe to retry.",
        )
    return candidate


def _validated_retry_upload_size(raw: Any, path: Path) -> int:
    source = raw if isinstance(raw, Mapping) else {}
    expected_size = source.get("size_bytes")
    try:
        current_size = path.stat().st_size
    except OSError as exc:
        raise RetryActionError(
            "source_missing",
            "The upload source file is no longer available.",
        ) from exc
    if (
        isinstance(expected_size, int)
        and not isinstance(expected_size, bool)
        and expected_size >= 0
        and current_size != expected_size
    ):
        raise RetryActionError(
            "source_changed",
            "The upload source changed after the original job was created.",
        )
    return current_size


def _frozen_retry_upload_metadata(
    raw: Any, path: Path
) -> Dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    expected_size = source.get("size_bytes")
    current_size = _validated_retry_upload_size(source, path)

    def safe_text(key: str, maximum: int) -> str:
        value = source.get(key)
        if not isinstance(value, str):
            return ""
        return "".join(
            character
            for character in value.strip()[:maximum]
            if ord(character) >= 32 and ord(character) != 127
        )

    streamer = dashboard_settings.canonical_streamer_login(
        source.get("streamer")
    )
    vod_id = safe_text("vod_id", 32)
    if vod_id and not vod_id.isdigit():
        vod_id = ""
    size_gb = source.get("size_gb")
    if (
        isinstance(size_gb, bool)
        or not isinstance(size_gb, (int, float))
        or not math.isfinite(float(size_gb))
        or size_gb < 0
    ):
        size_gb = round(current_size / (1024 ** 3), 3)
    return {
        "streamer": streamer,
        "date": safe_text("date", 64),
        "title": safe_text("title", 1000) or path.name,
        "vod_id": vod_id,
        "name": path.name,
        "size_bytes": (
            expected_size
            if isinstance(expected_size, int)
            and not isinstance(expected_size, bool)
            and expected_size >= 0
            else current_size
        ),
        "size_gb": float(size_gb),
        "youtube_playlist_id": _retry_playlist_id(
            source.get("youtube_playlist_id")
        ),
    }


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
        reason = str(retry.get("reason_code") or "not_retryable")
        return jsonify({
            "error": retry.get("reason"),
            "reason": reason,
            "outcome_uncertain": reason == "review_required",
        }), 409
    if not retry.get("reserved"):
        return jsonify({
            "ok": True,
            "duplicate": True,
            "pending": bool(retry.get("pending")),
            "retry_job_id": retry.get("retry_job_id") or "",
        })
    try:
        if retry.get("type") == "youtube_upload":
            settings = load_settings()
            path = _validated_retry_upload_path(retry["value"], settings)
            _validated_retry_upload_size(
                retry.get("item_metadata"), path
            )
            upload_kwargs: Dict[str, Any] = {
                "retry_of": {"job_id": job_id, "item_id": item_id}
            }
            if retry.get("interrupted"):
                upload_kwargs.update({
                    "playlist_id": _retry_playlist_id(
                        retry.get("playlist_id")
                    ),
                    "frozen_item_metadata": [
                        _frozen_retry_upload_metadata(
                            retry.get("item_metadata"), path
                        )
                    ],
                })
            retry_job_id = create_upload_job(
                [str(path)],
                "Retry YouTube Upload",
                **upload_kwargs,
            )
        else:
            retry_job_id = create_job(
                [_canonical_retry_download_url(retry["value"])],
                "Retry Twitch Download",
                retry_of={"job_id": job_id, "item_id": item_id},
            )
    except RetryActionError as exc:
        manager.cancel_retry_reservation(job_id, item_id)
        return jsonify({"error": str(exc), "reason": exc.reason}), 409
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        manager.cancel_retry_reservation(job_id, item_id)
        return jsonify({"error": str(exc), "reason": exc.code}), 409
    except RuntimeError:
        manager.cancel_retry_reservation(job_id, item_id)
        return jsonify({
            "error": "The retry job could not be created.",
            "reason": "not_retryable",
        }), 409
    except Exception:
        manager.cancel_retry_reservation(job_id, item_id)
        raise
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
    try:
        resolved = _job_manager_for_compatibility().resolve_error_by_id(
            job_id, item_id
        )
    except dashboard_jobs.JobPersistenceRequiredError as exc:
        return jsonify({"error": str(exc), "reason": exc.code}), 409
    if not resolved:
        return jsonify({"error": "Only an unresolved failed or interrupted item can be resolved."}), 409
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
