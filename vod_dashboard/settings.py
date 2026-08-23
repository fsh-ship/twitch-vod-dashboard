from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.runtime import (
    ARCHIVE_FILE_NAME,
    STREAMER_FILE_NAME,
    UPLOADED_VODS_FOLDER_NAME,
    RuntimePaths,
)


YTDLP_DEFAULT_OUTPUT_TEMPLATE = (
    "%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s"
)
MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = "{date_de} - {streamer} - {title}"


def _default_settings(runtime_paths: RuntimePaths) -> Dict[str, Any]:
    return {
        "download_path": str(runtime_paths.media_root),
        "streamer_file": str(runtime_paths.streamer_file),
        "archive_file": str(runtime_paths.archive_file),
        "cookie_browser": "",
        "cookie_file": "",
        "quality": "source/best",
        "fragments": 8,
        "twitch_rate_limit": "",
        "playlist_end": 150,
        "include_unknown_dates": True,
        "exclude_live_streams": True,
        "only_real_vod_urls": True,
        "strict_date_filter": False,
        "enrich_vod_dates": True,
        "output_template": YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        "merge_format": "mp4",
        "youtube_enabled": False,
        "youtube_auto_upload": False,
        "batch_postprocess_mode": "after_each",
        "youtube_privacy_status": "private",
        "youtube_playlist_id": "",
        "auto_recorder_enabled": False,
        "streamer_profiles": {},
        "youtube_client_secret_file": str(
            runtime_paths.youtube_client_secret_file
        ),
        "youtube_token_file": str(runtime_paths.youtube_token_file),
        "youtube_description": "Automatically uploaded by Twitch VOD Dashboard.",
        "youtube_tags": "twitch,vod",
        "youtube_category_id": "20",
        "youtube_chunk_size_mb": 64,
        "youtube_uploaded_files": [],
        "youtube_upload_history": [],
        "move_uploaded_vods": True,
        "uploaded_vods_folder": str(runtime_paths.uploaded_vods_folder),
        "youtube_title_template": "{streamer} VOD - {date_de} - {title}",
        "youtube_description_template": "Automatically archived Twitch VOD.\n\nStreamer: {streamer}\nDate: {date_de}\nOriginal: {url}\nVOD ID: {vod_id}\nDuration: {duration}\n\nPrivate archive.",
        "youtube_upload_mode": "stable",
        "manual_upload_prepare_enabled": True,
        "manual_upload_rename_video": True,
        "manual_upload_filename_template": MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
        "manual_upload_write_description": True,
        "manual_upload_write_metadata_json": True,
    }


_MODULE_RUNTIME_PATHS = RuntimePaths.from_environment(
    Path(__file__).resolve().parent.parent
)
DEFAULT_SETTINGS: Dict[str, Any] = _default_settings(_MODULE_RUNTIME_PATHS)


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "an"}:
        return True
    if text in {"0", "false", "no", "nein", "off", "aus", ""}:
        return False
    return default


def clean_batch_postprocess_mode(value: Any) -> str:
    value = str(value or "").strip()
    if value in {"after_each", "after_all"}:
        return value
    return "after_each"


def legacy_settings_candidates(
    settings_file: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> List[Path]:
    """Return the single explicitly configured legacy settings file, if valid."""
    env = os.environ if environ is None else environ
    raw = str(env.get("VOD_DASHBOARD_LEGACY_SETTINGS_PATH") or "").strip()
    if not raw:
        return []

    try:
        candidate = Path(raw).expanduser().resolve(strict=False)
        if candidate == settings_file.resolve(strict=False):
            return []
        if candidate.exists() and candidate.is_file():
            return [candidate]
    except Exception:
        pass
    return []


def read_json_file(
    path: Path,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8-sig")
            return json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        if log:
            try:
                log(f"Could not read {path}: {exc}")
            except Exception:
                pass
    return {}


def clean_stale_packaged_paths(
    settings: Dict[str, Any],
    default_dashboard_dir: Path,
    streamer_file_name: str = STREAMER_FILE_NAME,
    archive_file_name: str = ARCHIVE_FILE_NAME,
) -> Dict[str, Any]:
    """Remove paths left by old packaged/container builds."""

    def stale(value: Any) -> bool:
        text = str(value or "")
        return (
            text.startswith("/mnt/data")
            or text.startswith("/home/oai")
            or "\\mnt\\data" in text
        )

    if stale(settings.get("download_path")):
        settings["download_path"] = str(default_dashboard_dir)
    if stale(settings.get("streamer_file")):
        settings["streamer_file"] = str(
            Path(settings["download_path"]) / streamer_file_name
        )
    if stale(settings.get("archive_file")):
        settings["archive_file"] = str(
            Path(settings["download_path"]) / archive_file_name
        )
    if stale(settings.get("youtube_client_secret_file")):
        settings["youtube_client_secret_file"] = str(
            Path(settings["download_path"]) / "client_secret.json"
        )
    if stale(settings.get("youtube_token_file")):
        settings["youtube_token_file"] = str(
            Path(settings["download_path"]) / "youtube-token.json"
        )
    return settings


def fix_template_confusion(
    settings: Dict[str, Any],
    ytdlp_default_output_template: str = YTDLP_DEFAULT_OUTPUT_TEMPLATE,
    manual_upload_default_filename_template: str = (
        MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
    ),
) -> Dict[str, Any]:
    """Keep yt-dlp and final YouTube filename templates separate."""
    output_template = str(settings.get("output_template") or "").strip()
    manual_template = str(
        settings.get("manual_upload_filename_template") or ""
    ).strip()

    portable_template = output_template.replace("\\", "/")
    unsafe_output_path = (
        portable_template.startswith("/")
        or bool(re.match(r"^[A-Za-z]:", output_template))
        or ".." in portable_template.split("/")
    )
    if (
        not output_template
        or ("{" in output_template and "%(" not in output_template)
        or unsafe_output_path
    ):
        settings["output_template"] = ytdlp_default_output_template

    if not manual_template or "%(" in manual_template:
        settings[
            "manual_upload_filename_template"
        ] = manual_upload_default_filename_template
    return settings


def force_user_data_paths(
    settings: Dict[str, Any],
    media_policy: MediaPathPolicy,
    fixed_streamer_file: Path,
    fixed_archive_file: Path,
    fixed_uploaded_vods_folder: Path,
) -> Dict[str, Any]:
    download_raw = str(settings.get("download_path") or "").strip()
    settings["download_path"] = str(
        media_policy.normalize_media_directory(download_raw, media_policy.media_root)
    )
    settings["streamer_file"] = str(fixed_streamer_file)
    settings["archive_file"] = str(fixed_archive_file)

    uploaded_raw = str(settings.get("uploaded_vods_folder") or "").strip()
    settings["uploaded_vods_folder"] = str(
        media_policy.normalize_media_directory(
            uploaded_raw, fixed_uploaded_vods_folder
        )
    )
    return settings


def normalize_settings(
    settings: Dict[str, Any],
    *,
    media_policy: MediaPathPolicy,
    default_dashboard_dir: Path,
    fixed_streamer_file: Path,
    fixed_archive_file: Path,
    fixed_uploaded_vods_folder: Path,
    environ: Optional[Mapping[str, str]] = None,
    streamer_file_name: str = STREAMER_FILE_NAME,
    archive_file_name: str = ARCHIVE_FILE_NAME,
    uploaded_vods_folder_name: str = UPLOADED_VODS_FOLDER_NAME,
    ytdlp_default_output_template: str = YTDLP_DEFAULT_OUTPUT_TEMPLATE,
    manual_upload_default_filename_template: str = (
        MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
    ),
) -> Dict[str, Any]:
    env = os.environ if environ is None else environ
    settings["fragments"] = max(1, to_int(settings.get("fragments"), 8))
    settings["playlist_end"] = max(
        1, to_int(settings.get("playlist_end"), 150)
    )
    settings["youtube_chunk_size_mb"] = max(
        1, to_int(settings.get("youtube_chunk_size_mb"), 64)
    )
    for key, default in {
        "include_unknown_dates": True,
        "exclude_live_streams": True,
        "only_real_vod_urls": True,
        "strict_date_filter": False,
        "enrich_vod_dates": True,
        "youtube_enabled": False,
        "youtube_auto_upload": False,
        "auto_recorder_enabled": False,
        "manual_upload_prepare_enabled": True,
        "manual_upload_rename_video": True,
        "manual_upload_write_description": True,
        "manual_upload_write_metadata_json": True,
        "move_uploaded_vods": True,
    }.items():
        settings[key] = to_bool(settings.get(key), default)

    old_base = str(settings.get("base_path") or "").strip()
    if old_base and not settings.get("download_path"):
        settings["download_path"] = old_base

    if not str(settings.get("download_path") or "").strip():
        settings["download_path"] = str(media_policy.media_root)
    if not str(settings.get("streamer_file") or "").strip():
        settings["streamer_file"] = str(
            Path(settings["download_path"]) / streamer_file_name
        )
    if not str(settings.get("archive_file") or "").strip():
        settings["archive_file"] = str(
            Path(settings["download_path"]) / archive_file_name
        )
    if not str(settings.get("youtube_client_secret_file") or "").strip():
        settings["youtube_client_secret_file"] = str(
            Path(settings["download_path"]) / "client_secret.json"
        )
    if not str(settings.get("youtube_token_file") or "").strip():
        settings["youtube_token_file"] = str(
            Path(settings["download_path"]) / "youtube-token.json"
        )
    if not str(settings.get("uploaded_vods_folder") or "").strip():
        settings["uploaded_vods_folder"] = str(
            Path(settings["download_path"]) / uploaded_vods_folder_name
        )
    settings["twitch_rate_limit"] = str(
        settings.get("twitch_rate_limit") or ""
    ).strip()
    configured_cookie_file = str(
        env.get("VOD_DASHBOARD_TWITCH_COOKIE_FILE") or ""
    ).strip()
    if configured_cookie_file:
        settings["cookie_file"] = configured_cookie_file
    settings["batch_postprocess_mode"] = clean_batch_postprocess_mode(
        settings.get("batch_postprocess_mode")
    )
    settings["streamer_profiles"] = normalize_streamer_profiles(
        settings.get("streamer_profiles")
    )

    return force_user_data_paths(
        fix_template_confusion(
            clean_stale_packaged_paths(
                settings,
                default_dashboard_dir,
                streamer_file_name,
                archive_file_name,
            ),
            ytdlp_default_output_template,
            manual_upload_default_filename_template,
        ),
        media_policy,
        fixed_streamer_file,
        fixed_archive_file,
        fixed_uploaded_vods_folder,
    )


def streamer_file(fixed_streamer_file: Path) -> Path:
    return fixed_streamer_file.expanduser()


def legacy_streamer_candidates(
    app_dir: Path,
    fixed_streamer_file: Path,
) -> List[Path]:
    """Find old streamer files for diagnostics without importing them."""
    candidates: List[Path] = []

    def add(path: Path) -> None:
        try:
            if (
                path.exists()
                and path.is_file()
                and path.resolve() != fixed_streamer_file.resolve()
                and path not in candidates
            ):
                candidates.append(path)
        except Exception:
            pass

    for base in [app_dir, app_dir.parent, app_dir.parent.parent]:
        try:
            if base.exists():
                for path in base.glob("**/streamer.txt"):
                    try:
                        if len(path.relative_to(base).parts) <= 6:
                            add(path)
                    except Exception:
                        add(path)
        except Exception:
            pass

    try:
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        pass
    return candidates


def clean_streamer_names(names: List[str]) -> List[str]:
    clean: List[str] = []
    seen = set()
    for raw in names:
        name = str(raw or "").strip().lstrip("@")
        if not name or name.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", name):
            continue
        key = name.lower()
        if key not in seen:
            clean.append(name)
            seen.add(key)
    return clean


def canonical_streamer_login(value: Any) -> str:
    """Return a canonical Twitch login using the streamer-list rules."""
    names = clean_streamer_names([value])
    return names[0].lower() if names else ""


def normalize_streamer_profiles(value: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize the allowlisted per-streamer settings mapping."""
    if not isinstance(value, Mapping):
        return {}

    profiles: Dict[str, Dict[str, Any]] = {}
    for raw_login, raw_profile in value.items():
        login = canonical_streamer_login(raw_login)
        if not login or not isinstance(raw_profile, Mapping):
            continue

        profile: Dict[str, Any] = {}
        playlist_id = str(
            raw_profile.get("youtube_playlist_id") or ""
        ).strip()
        if playlist_id:
            profile["youtube_playlist_id"] = playlist_id
        if raw_profile.get("auto_record") is True:
            profile["auto_record"] = True
        if profile and login not in profiles:
            profiles[login] = profile
    return profiles


def streamer_profile_for(
    settings: Mapping[str, Any], streamer_login: Any
) -> Dict[str, Any]:
    """Look up a normalized profile without exposing mutable settings state."""
    login = canonical_streamer_login(streamer_login)
    if not login:
        return {}
    profile = normalize_streamer_profiles(
        settings.get("streamer_profiles")
    ).get(login)
    return dict(profile or {})


def resolve_youtube_playlist_for_streamer(
    settings: Mapping[str, Any],
    streamer_login: Any,
    *,
    explicit_playlist: Optional[Any] = None,
) -> str:
    """Resolve one upload item's playlist without mutating settings."""
    if explicit_playlist is not None:
        return str(explicit_playlist or "").strip()

    profile = streamer_profile_for(settings, streamer_login)
    profile_playlist = str(
        profile.get("youtube_playlist_id") or ""
    ).strip()
    if profile_playlist:
        return profile_playlist
    return str(settings.get("youtube_playlist_id") or "").strip()


def read_streamers_from_path(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")

    raw = raw.replace("\\\\r\\\\n", "\n").replace("\\\\n", "\n")
    raw = raw.replace("\\r\\n", "\n").replace("\\n", "\n")
    raw = raw.replace(",", "\n").replace(";", "\n")
    return clean_streamer_names(raw.splitlines())


def write_streamers_to_path(path: Path, names: List[str]) -> List[str]:
    clean = clean_streamer_names(names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(clean) + ("\n" if clean else ""), encoding="utf-8"
    )
    return clean


def archive_file(fixed_archive_file: Path) -> Path:
    return fixed_archive_file.expanduser()


def archive_ids_from_path(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        lines = path.read_text(
            encoding="utf-8-sig", errors="ignore"
        ).splitlines()
    except Exception:
        lines = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        ids.add(raw)
        for part in raw.split():
            ids.add(part)
            match = re.search(r"(\d{6,})", part)
            if match:
                ids.add(match.group(1))
        match = re.search(r"(\d{6,})", raw)
        if match:
            ids.add(match.group(1))
    return ids


@dataclass(frozen=True)
class RuntimeDataRepository:
    app_dir: Path
    default_dashboard_dir: Path
    media_policy: MediaPathPolicy
    fixed_streamer_file: Path
    fixed_archive_file: Path
    fixed_uploaded_vods_folder: Path
    settings_loader: Optional[Callable[[], Dict[str, Any]]] = None

    def _settings(
        self, settings: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if settings:
            return settings
        if self.settings_loader:
            return self.settings_loader()
        raise RuntimeError(
            "A settings loader is required when settings are not provided."
        )

    def streamer_file(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> Path:
        del settings
        return streamer_file(self.fixed_streamer_file)

    def legacy_streamer_candidates(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> List[Path]:
        del settings
        return legacy_streamer_candidates(
            self.app_dir, self.fixed_streamer_file
        )

    def read_streamers_from_path(self, path: Path) -> List[str]:
        return read_streamers_from_path(path)

    def write_streamers_to_path(
        self, path: Path, names: List[str]
    ) -> List[str]:
        return write_streamers_to_path(path, names)

    def archive_file(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> Path:
        del settings
        return archive_file(self.fixed_archive_file)

    def archive_ids(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> set[str]:
        resolved_settings = self._settings(settings)
        return archive_ids_from_path(self.archive_file(resolved_settings))

    def ensure_files(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> None:
        resolved_settings = self._settings(settings)
        self.default_dashboard_dir.mkdir(parents=True, exist_ok=True)
        self.media_policy.download_path(resolved_settings).mkdir(
            parents=True, exist_ok=True
        )
        streamer_path = self.streamer_file(resolved_settings)
        archive_path = self.archive_file(resolved_settings)
        streamer_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        streamer_path.touch(exist_ok=True)
        archive_path.touch(exist_ok=True)
        if resolved_settings.get("move_uploaded_vods", True):
            self.media_policy.uploaded_vods_folder(
                resolved_settings, self.fixed_uploaded_vods_folder
            ).mkdir(parents=True, exist_ok=True)

    def read_streamers(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        resolved_settings = self._settings(settings)
        self.ensure_files(resolved_settings)
        return self.read_streamers_from_path(
            self.streamer_file(resolved_settings)
        )

    def write_streamers(
        self,
        names: List[str],
        settings: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        resolved_settings = self._settings(settings)
        self.ensure_files(resolved_settings)
        return self.write_streamers_to_path(
            self.streamer_file(resolved_settings), names
        )


@dataclass(frozen=True)
class SettingsRepository:
    settings_file: Path
    media_policy: MediaPathPolicy
    default_settings: Mapping[str, Any]
    default_dashboard_dir: Path
    fixed_streamer_file: Path
    fixed_archive_file: Path
    fixed_uploaded_vods_folder: Path
    environ: Optional[Mapping[str, str]] = None
    log: Optional[Callable[[str], None]] = None
    ensure_files: Optional[Callable[[Dict[str, Any]], None]] = None
    streamer_file_name: str = STREAMER_FILE_NAME
    archive_file_name: str = ARCHIVE_FILE_NAME
    uploaded_vods_folder_name: str = UPLOADED_VODS_FOLDER_NAME
    ytdlp_default_output_template: str = YTDLP_DEFAULT_OUTPUT_TEMPLATE
    manual_upload_default_filename_template: str = (
        MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE
    )

    def legacy_settings_candidates(self) -> List[Path]:
        return legacy_settings_candidates(self.settings_file, self.environ)

    def read_json_file(self, path: Path) -> Dict[str, Any]:
        return read_json_file(path, self.log)

    def normalize(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        return normalize_settings(
            settings,
            media_policy=self.media_policy,
            default_dashboard_dir=self.default_dashboard_dir,
            fixed_streamer_file=self.fixed_streamer_file,
            fixed_archive_file=self.fixed_archive_file,
            fixed_uploaded_vods_folder=self.fixed_uploaded_vods_folder,
            environ=self.environ,
            streamer_file_name=self.streamer_file_name,
            archive_file_name=self.archive_file_name,
            uploaded_vods_folder_name=self.uploaded_vods_folder_name,
            ytdlp_default_output_template=self.ytdlp_default_output_template,
            manual_upload_default_filename_template=(
                self.manual_upload_default_filename_template
            ),
        )

    def load(self) -> Dict[str, Any]:
        data = self.read_json_file(self.settings_file)
        if not self.settings_file.exists():
            for candidate in self.legacy_settings_candidates():
                legacy = self.read_json_file(candidate)
                if legacy:
                    data = legacy
                    if self.log:
                        try:
                            self.log(
                                f"Loaded legacy settings from {candidate}; they will be migrated to {self.settings_file} on the next save."
                            )
                        except Exception:
                            pass
                    break
        return self.normalize({**self.default_settings, **data})

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        current = self.load()
        allowed = set(self.default_settings.keys())
        path_keys = {
            "download_path",
            "streamer_file",
            "archive_file",
            "youtube_client_secret_file",
            "youtube_token_file",
            "uploaded_vods_folder",
        }
        for key, value in data.items():
            if key in allowed:
                if key in path_keys:
                    if str(value or "").strip():
                        current[key] = value
                else:
                    current[key] = value

        current = self.normalize(current)
        download_dir = self.media_policy.download_path(current)
        download_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        check = self.read_json_file(self.settings_file)
        if not check:
            raise RuntimeError(
                f"Settings were written but could not be read back: {self.settings_file}"
            )

        if self.ensure_files:
            self.ensure_files(current)
        return self.normalize({**self.default_settings, **check})
