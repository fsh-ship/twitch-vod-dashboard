from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional


STREAMER_FILE_NAME = "streamer.txt"
ARCHIVE_FILE_NAME = "archive.txt"
UPLOADED_VODS_FOLDER_NAME = "_hochgeladen"
LOG_MAX_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    user_home: Path
    default_media_root: Path
    media_root: Path
    dashboard_dir: Path
    settings_file: Path
    local_settings_file: Path
    log_file: Path
    streamer_file: Path
    archive_file: Path
    youtube_client_secret_file: Path
    youtube_token_file: Path
    uploaded_vods_folder: Path

    @classmethod
    def from_environment(
        cls,
        app_dir: Path,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "RuntimePaths":
        env = os.environ if environ is None else environ
        resolved_app_dir = Path(app_dir)
        user_home = Path(env.get("USERPROFILE") or str(Path.home()))
        default_media_root = user_home / "Documents" / "Twitch VODs"
        media_root = Path(
            env.get("VOD_DASHBOARD_MEDIA_ROOT") or default_media_root
        ).expanduser().resolve()
        dashboard_dir = Path(env.get("VOD_DASHBOARD_DIR") or media_root)

        return cls(
            app_dir=resolved_app_dir,
            user_home=user_home,
            default_media_root=default_media_root,
            media_root=media_root,
            dashboard_dir=dashboard_dir,
            settings_file=Path(
                env.get("VOD_DASHBOARD_SETTINGS")
                or (dashboard_dir / "dashboard-settings.json")
            ),
            local_settings_file=resolved_app_dir / "settings.json",
            log_file=Path(
                env.get("VOD_DASHBOARD_LOG_FILE")
                or (resolved_app_dir / "dashboard.log")
            ),
            streamer_file=dashboard_dir / STREAMER_FILE_NAME,
            archive_file=dashboard_dir / ARCHIVE_FILE_NAME,
            youtube_client_secret_file=dashboard_dir / "client_secret.json",
            youtube_token_file=dashboard_dir / "youtube-token.json",
            uploaded_vods_folder=media_root / UPLOADED_VODS_FOLDER_NAME,
        )


log_file_lock = threading.Lock()


def log_line(
    text: str,
    log_file: Path,
    max_bytes: int = LOG_MAX_BYTES,
) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file_lock:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            should_rotate = log_file.stat().st_size >= max_bytes
        except FileNotFoundError:
            should_rotate = False
        if should_rotate:
            backup_file = Path(f"{log_file}.1")
            backup_file.unlink(missing_ok=True)
            log_file.replace(backup_file)
        with log_file.open("a", encoding="utf-8") as file_handle:
            file_handle.write(f"[{stamp}] {text}\n")
