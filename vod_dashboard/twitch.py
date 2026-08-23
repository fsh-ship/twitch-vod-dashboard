from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def entry_date(entry: Dict[str, Any]) -> Optional[str]:
    for key in (
        "upload_date",
        "release_date",
        "timestamp",
        "release_timestamp",
    ):
        val = entry.get(key)
        if not val:
            continue
        if key in {"timestamp", "release_timestamp"}:
            try:
                return datetime.fromtimestamp(int(val), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                continue
        dt = parse_date(str(val))
        if dt:
            return dt.strftime("%Y-%m-%d")
    return None


def normalize_vod_url(entry: Any) -> str:
    if not isinstance(entry, dict):
        return canonical_twitch_vod_url(entry)

    canonical = canonical_twitch_vod_url(entry)
    if canonical:
        return canonical

    url = str(entry.get("url") or entry.get("webpage_url") or "")
    if not url:
        return ""
    if url.startswith("/videos/"):
        return "https://www.twitch.tv" + url
    if url.startswith("videos/"):
        return "https://www.twitch.tv/" + url
    if is_real_vod_url(url):
        return url
    return ""


def canonical_twitch_vod_url(entry_or_url: Any) -> str:
    """Gibt eine saubere Twitch-VOD-URL zurück, wenn eine VOD-ID erkennbar ist."""
    vod_id = extract_twitch_vod_id(entry_or_url)
    if vod_id:
        return f"https://www.twitch.tv/videos/{vod_id}"
    if isinstance(entry_or_url, dict):
        raw = str(entry_or_url.get("webpage_url") or entry_or_url.get("url") or "")
    else:
        raw = str(entry_or_url or "")
    if is_real_vod_url(raw):
        return raw
    return ""


def is_real_vod_url(url: str) -> bool:
    return bool(re.search(r"https?://(?:www\.)?twitch\.tv/videos/\d+", str(url)))


def is_live_or_upcoming_entry(entry: Dict[str, Any]) -> bool:
    """Filtert aktuelle Lives/Upcoming heraus. Fertige VODs mit was_live=True bleiben erlaubt."""
    live_status = str(entry.get("live_status") or "").strip().lower()
    if live_status in {"is_live", "is_upcoming", "is_live_notification", "is_upcoming_notification"}:
        return True
    if entry.get("is_live") is True:
        return True
    if entry.get("is_upcoming") is True:
        return True
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    if url and "twitch.tv/" in url and "/videos/" not in url and not extract_twitch_vod_id(entry):
        return True
    return False


def vod_id_from_url(url: str) -> str:
    prefixed = re.fullmatch(r"v(\d{6,})", str(url or "").strip(), re.I)
    if prefixed:
        return prefixed.group(1)
    match = re.search(r"(?:videos/|video=|v=)(\d{6,})", str(url or ""))
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{8,})\b", str(url or ""))
    return match.group(1) if match else ""


def extract_twitch_vod_id(entry_or_url: Any) -> str:
    """Findet eine Twitch-VOD-ID auch dann, wenn yt-dlp nur id/display_id/interne URLs liefert."""
    if isinstance(entry_or_url, dict):
        candidates = [
            entry_or_url.get("id"),
            entry_or_url.get("display_id"),
            entry_or_url.get("url"),
            entry_or_url.get("webpage_url"),
            entry_or_url.get("original_url"),
            entry_or_url.get("video_id"),
            entry_or_url.get("vod_id"),
        ]
    else:
        candidates = [entry_or_url]

    for value in candidates:
        if value is None:
            continue
        s = str(value)
        prefixed = re.fullmatch(r"v(\d{6,})", s.strip(), re.I)
        if prefixed:
            return prefixed.group(1)
        match = re.search(r"(?:videos/|video=|v=)(\d{6,})", s)
        if match:
            return match.group(1)
        if re.fullmatch(r"\d{6,}", s):
            return s
        match = re.search(r"\b(\d{8,})\b", s)
        if match:
            return match.group(1)
    return ""


def in_range(
    date_str: str,
    start: Optional[datetime],
    end: Optional[datetime],
    include_unknown: bool = True,
) -> bool:
    if not date_str or str(date_str).lower() == "unbekannt":
        return bool(include_unknown)
    dt = parse_date(date_str)
    if not dt:
        return bool(include_unknown)
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def normalize_single_vod_url(raw: str) -> str:
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if re.fullmatch(r"\d{6,}", s):
        return f"https://www.twitch.tv/videos/{s}"
    if s.startswith("www."):
        s = "https://" + s
    if s.startswith("twitch.tv/"):
        s = "https://www." + s
    s = s.replace("https://m.twitch.tv/", "https://www.twitch.tv/")
    s = s.replace("http://www.twitch.tv/", "https://www.twitch.tv/")
    match = re.search(r"(?:videos/|video=|v=)(\d{6,})", s)
    if match:
        return f"https://www.twitch.tv/videos/{match.group(1)}"
    return ""


def validate_single_vod_url(raw: str) -> Dict[str, Any]:
    url = normalize_single_vod_url(raw)
    if not url:
        return {
            "ok": False,
            "error": "Enter a valid Twitch VOD link, for example https://www.twitch.tv/videos/1234567890",
            "url": "",
            "vod_id": "",
        }
    match = re.search(r"videos/(\d{6,})", url)
    return {"ok": True, "url": url, "vod_id": match.group(1) if match else ""}


def ytdlp_base_command(python_executable: Optional[str] = None) -> List[str]:
    return [python_executable or sys.executable, "-m", "yt_dlp"]


def ytdlp_cookie_args(settings: Dict[str, Any]) -> List[str]:
    """Prefer a configured cookie file and use browser cookies as fallback."""
    cookie_file = str(settings.get("cookie_file") or "").strip()
    if cookie_file:
        cookie_path = Path(cookie_file).expanduser()
        if not cookie_path.exists():
            raise RuntimeError(f"Cookie file not found: {cookie_path}")
        return ["--cookies", str(cookie_path)]

    cookie_browser = str(settings.get("cookie_browser") or "").strip()
    if cookie_browser:
        return ["--cookies-from-browser", cookie_browser]
    return []


def clean_twitch_rate_limit(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    value = value.replace(" ", "")
    if re.fullmatch(r"\d+(?:\.\d+)?[KkMmGg]?", value):
        return value.upper()
    return ""


def _command_parts(
    settings: Dict[str, Any],
    command_factory: Optional[Callable[[], List[str]]],
    cookie_args_factory: Optional[Callable[[Dict[str, Any]], List[str]]],
) -> tuple[List[str], List[str]]:
    command = (
        command_factory() if command_factory else ytdlp_base_command()
    )
    cookie_arguments = (
        cookie_args_factory(settings)
        if cookie_args_factory
        else ytdlp_cookie_args(settings)
    )
    return command, cookie_arguments


_TWITCH_LOGIN_RE = re.compile(r"[A-Za-z0-9_]{1,25}")
_TWITCH_NOT_LIVE_RE = re.compile(
    r"\b(?:the\s+)?(?:channel|user)\s+is\s+not\s+currently\s+live\b",
    re.IGNORECASE,
)
LIVE_STATUS_TIMEOUT_SECONDS = 30
VOD_DETAIL_TIMEOUT_SECONDS = 30
VOD_DATE_ENRICHMENT_WORKERS = 2
_TWITCH_QUALITY_RE = re.compile(
    r"(?<!\d)(\d{3,4})p(?:([1-9]\d{1,2}))?\b", re.IGNORECASE
)
LIVE_RECORDING_OUTPUT_MARKER = "VOD-DASHBOARD-RECORDING-FILE="
LIVE_RECORDING_FILENAME_TEMPLATE = (
    "%(upload_date)s - %(uploader)s - LIVE - %(title)s [%(id)s].%(ext)s"
)


def _canonical_live_streamer_login(value: Any) -> str:
    login = str(value or "").strip().lstrip("@").lower()
    return login if _TWITCH_LOGIN_RE.fullmatch(login) else ""


def _live_started_at(metadata: Dict[str, Any]) -> Optional[str]:
    timestamp = metadata.get("timestamp")
    if timestamp is not None and timestamp != "":
        try:
            numeric = float(timestamp)
            if numeric >= 0:
                return datetime.fromtimestamp(
                    numeric, tz=timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    upload_date = parse_date(str(metadata.get("upload_date") or ""))
    return upload_date.strftime("%Y-%m-%d") if upload_date else None


def _live_quality_labels(formats: Any) -> List[str]:
    labels: set[str] = set()
    for raw_format in formats if isinstance(formats, list) else []:
        if not isinstance(raw_format, dict):
            continue
        format_id = str(raw_format.get("format_id") or "").strip()
        format_note = str(raw_format.get("format_note") or "").strip()
        if (
            str(raw_format.get("vcodec") or "").lower() == "none"
            or "audio only" in format_note.lower()
            or format_id.lower() in {"audio", "audio_only", "storyboard"}
        ):
            continue

        descriptive = " ".join(
            filter(
                None,
                (
                    format_id,
                    format_note,
                    str(raw_format.get("resolution") or "").strip(),
                ),
            )
        )
        if "source" in descriptive.lower() or format_id.lower() == "chunked":
            labels.add("Source")
            continue

        match = _TWITCH_QUALITY_RE.search(descriptive)
        if match:
            height = int(match.group(1))
            try:
                frame_rate = int(
                    match.group(2) or round(float(raw_format.get("fps") or 0))
                )
            except (TypeError, ValueError, OverflowError):
                frame_rate = 0
        else:
            try:
                height = int(float(raw_format.get("height")))
            except (TypeError, ValueError, OverflowError):
                continue
            try:
                frame_rate = int(round(float(raw_format.get("fps") or 0)))
            except (TypeError, ValueError, OverflowError):
                frame_rate = 0

        if height <= 0:
            continue
        suffix = str(frame_rate) if frame_rate >= 50 else ""
        labels.add(f"{height}p{suffix}")

    def sort_key(label: str) -> tuple[int, int, int]:
        if label == "Source":
            return (0, 0, 0)
        match = _TWITCH_QUALITY_RE.fullmatch(label)
        return (
            1,
            -int(match.group(1)) if match else 0,
            -int(match.group(2) or 0) if match else 0,
        )

    return sorted(labels, key=sort_key)


def run_ytdlp_live_status(
    streamer: str,
    settings: Dict[str, Any],
    *,
    command_factory: Optional[Callable[[], List[str]]] = None,
    cookie_args_factory: Optional[
        Callable[[Dict[str, Any]], List[str]]
    ] = None,
) -> Dict[str, Any]:
    """Return a safe, read-only status payload for one Twitch channel."""
    canonical_login = _canonical_live_streamer_login(streamer)
    if not canonical_login:
        raise ValueError("A valid Twitch streamer login is required.")

    base_command, cookie_arguments = _command_parts(
        settings, command_factory, cookie_args_factory
    )
    channel_url = f"https://www.twitch.tv/{canonical_login}"
    command = base_command + [
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
    ]
    command.extend(cookie_arguments)
    command.append(channel_url)

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=LIVE_STATUS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The Twitch live-status query timed out.") from exc

    combined_output = "\n".join(
        filter(None, (process.stderr or "", process.stdout or ""))
    )
    if process.returncode != 0:
        if _TWITCH_NOT_LIVE_RE.search(combined_output):
            return {"streamer": canonical_login, "state": "offline"}
        raise RuntimeError(
            f"The Twitch live-status query failed with code {process.returncode}."
        )

    if not (process.stdout or "").strip():
        raise RuntimeError("The Twitch live-status query returned no metadata.")
    try:
        metadata = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The Twitch live-status query returned invalid metadata."
        ) from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("The Twitch live-status query returned invalid metadata.")

    live_status = str(metadata.get("live_status") or "").strip().lower()
    if metadata.get("is_live") is not True and live_status != "is_live":
        if metadata.get("is_live") is False or live_status in {
            "not_live",
            "post_live",
            "was_live",
        }:
            return {"streamer": canonical_login, "state": "offline"}
        raise RuntimeError(
            "The Twitch live-status query did not return a definitive live state."
        )

    broadcast_title = str(metadata.get("description") or "").strip()
    if not broadcast_title:
        broadcast_title = str(metadata.get("title") or "").strip()
    display_name = str(
        metadata.get("uploader") or metadata.get("channel") or canonical_login
    ).strip()

    return {
        "streamer": canonical_login,
        "state": "live",
        "display_name": display_name,
        "stream_id": str(metadata.get("id") or "").strip(),
        "title": broadcast_title,
        "started_at": _live_started_at(metadata),
        "qualities": _live_quality_labels(metadata.get("formats")),
    }


def live_recording_output_template(streamer: str, *, attempt: int = 1) -> str:
    """Return the fixed, server-controlled relative output template."""
    canonical_login = _canonical_live_streamer_login(streamer)
    if canonical_login != str(streamer or "").strip():
        raise ValueError("A normalized Twitch streamer login is required.")
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or attempt > 1000
    ):
        raise ValueError("A valid recording attempt is required.")
    filename = LIVE_RECORDING_FILENAME_TEMPLATE
    if attempt > 1:
        filename = filename.replace(
            ".%(ext)s", f" - RETRY {attempt}.%(ext)s"
        )
    return f"{canonical_login}/{filename}"


def build_live_recording_command(
    streamer: str,
    settings: Dict[str, Any],
    *,
    attempt: int = 1,
    download_directory: Path,
    command_factory: Optional[Callable[[], List[str]]] = None,
    cookie_args_factory: Optional[
        Callable[[Dict[str, Any]], List[str]]
    ] = None,
) -> List[str]:
    """Build one read-only-input Twitch live-recording command."""
    output_template = live_recording_output_template(
        streamer, attempt=attempt
    )
    base_command, cookie_arguments = _command_parts(
        settings, command_factory, cookie_args_factory
    )
    quality = str(settings.get("quality") or "source/best").strip()
    merge_format = str(settings.get("merge_format") or "mp4").strip()
    channel_url = f"https://www.twitch.tv/{streamer}"
    return base_command + cookie_arguments + [
        "--no-playlist",
        "--downloader",
        "m3u8:ffmpeg",
        "-f",
        quality,
        "--write-info-json",
        "--print",
        f"after_move:{LIVE_RECORDING_OUTPUT_MARKER}%(filepath)s",
        "--no-quiet",
        "-P",
        str(Path(download_directory).resolve()),
        "-o",
        output_template,
        "--merge-output-format",
        merge_format,
        channel_url,
    ]


def build_download_command(
    urls: List[str],
    settings: Dict[str, Any],
    *,
    download_directory: Path,
    archive_path: Path,
    command_factory: Optional[Callable[[], List[str]]] = None,
    cookie_args_factory: Optional[
        Callable[[Dict[str, Any]], List[str]]
    ] = None,
) -> tuple[List[str], Path]:
    temporary = tempfile.NamedTemporaryFile(
        "w", delete=False, encoding="utf-8", suffix="-vods.txt"
    )
    with temporary:
        temporary.write("\n".join(urls))
        temporary.write("\n")
    list_path = Path(temporary.name)
    rate_limit = clean_twitch_rate_limit(settings.get("twitch_rate_limit"))
    base_command, cookie_arguments = _command_parts(
        settings, command_factory, cookie_args_factory
    )
    command = base_command + cookie_arguments + [
        "--ignore-errors",
        "--downloader",
        "m3u8:ffmpeg",
        "--print",
        "before_dl:VOD-DASHBOARD-DURATION=%(duration)s",
        "--no-quiet",
        "-a",
        str(list_path),
        "-N",
        str(settings["fragments"]),
        "-f",
        str(settings["quality"]),
        "--download-archive",
        str(archive_path),
        "--retries",
        "infinite",
        "--fragment-retries",
        "infinite",
        "--continue",
        "--write-info-json",
        "-P",
        str(download_directory),
        "-o",
        str(settings["output_template"]),
        "--merge-output-format",
        str(settings["merge_format"]),
    ]
    if rate_limit:
        command.extend(["--limit-rate", rate_limit])
    return command, list_path


def run_ytdlp_vod_detail(
    url: str,
    settings: Dict[str, Any],
    *,
    command_factory: Optional[Callable[[], List[str]]] = None,
    cookie_args_factory: Optional[
        Callable[[Dict[str, Any]], List[str]]
    ] = None,
) -> Dict[str, Any]:
    """Read detail metadata for one Twitch VOD."""
    base_command, cookie_arguments = _command_parts(
        settings, command_factory, cookie_args_factory
    )
    command = base_command + ["--dump-single-json", "--no-playlist"]
    command.extend(cookie_arguments)
    command.append(str(url))

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=VOD_DETAIL_TIMEOUT_SECONDS,
    )
    if process.returncode != 0:
        raise RuntimeError(
            (
                process.stderr
                or process.stdout
                or "yt-dlp detail query failed"
            )[-2000:]
        )
    raw = process.stdout or ""
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def run_ytdlp_json_sources(
    streamer: str,
    limit: Any = None,
    settings: Optional[Dict[str, Any]] = None,
    *,
    settings_loader: Optional[Callable[[], Dict[str, Any]]] = None,
    command_factory: Optional[Callable[[], List[str]]] = None,
    cookie_args_factory: Optional[
        Callable[[Dict[str, Any]], List[str]]
    ] = None,
) -> List[Dict[str, Any]]:
    """Query the existing Twitch source fallback sequence through yt-dlp."""
    if isinstance(limit, dict):
        settings, limit = limit, settings

    if not settings:
        if not settings_loader:
            raise RuntimeError(
                "A settings loader is required when settings are not provided."
            )
        settings = settings_loader()
    streamer = str(streamer or "").strip().lstrip("@")
    end = max(
        1,
        _to_int(
            limit or settings.get("playlist_end", 150),
            settings.get("playlist_end", 150),
        ),
    )
    urls = [
        f"https://www.twitch.tv/{streamer}/videos?filter=archives&sort=time",
        f"https://www.twitch.tv/{streamer}/videos?filter=all&sort=time",
        f"https://www.twitch.tv/{streamer}/videos",
    ]
    playlists: List[Dict[str, Any]] = []

    for source_url in urls:
        base_command, cookie_arguments = _command_parts(
            settings, command_factory, cookie_args_factory
        )
        command = base_command + [
            "--flat-playlist",
            "--dump-single-json",
            "--playlist-end",
            str(end),
        ]
        command.extend(cookie_arguments)
        command.append(source_url)
        source_info: Dict[str, Any] = {
            "_source_url": source_url,
            "_returncode": None,
            "_stderr": "",
            "entries": [],
        }

        try:
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=180
            )
            source_info["_returncode"] = process.returncode
            source_info["_stderr"] = (process.stderr or "")[-1200:]

            if process.returncode != 0 or not (process.stdout or "").strip():
                playlists.append(source_info)
                continue

            data = json.loads(process.stdout)
            raw_entries = data.get("entries") if isinstance(data, dict) else []
            if not isinstance(raw_entries, list):
                raw_entries = []

            normalized_entries: List[Dict[str, Any]] = []
            seen: set[str] = set()
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry)
                url = normalize_vod_url(entry)
                vod_id = extract_twitch_vod_id(entry) or vod_id_from_url(url)
                key = (
                    vod_id
                    or url
                    or str(
                        entry.get("id")
                        or entry.get("display_id")
                        or entry.get("title")
                        or ""
                    )
                )
                if not key or key in seen:
                    continue
                seen.add(key)

                if url:
                    entry["url"] = url
                    entry["webpage_url"] = url
                if vod_id:
                    entry["id"] = str(vod_id)
                normalized_entries.append(entry)

            source_info["entries"] = normalized_entries
            playlists.append(source_info)
        except Exception as exc:
            source_info["_returncode"] = -999
            source_info["_stderr"] = str(exc)
            playlists.append(source_info)

    return playlists


def run_ytdlp_json_for_streamer(
    streamer: str,
    settings: Dict[str, Any],
    limit: int,
    *,
    source_runner: Optional[
        Callable[[str, Any, Optional[Dict[str, Any]]], List[Dict[str, Any]]]
    ] = None,
) -> Dict[str, Any]:
    """Compatibility shape for the older per-streamer search helper."""
    runner = source_runner or run_ytdlp_json_sources
    playlists = runner(streamer, limit, settings)
    entries: List[Dict[str, Any]] = []
    for playlist in playlists:
        if isinstance(playlist, dict):
            for entry in playlist.get("entries") or []:
                if isinstance(entry, dict):
                    entries.append(entry)
    return {
        "id": streamer,
        "title": streamer,
        "entries": entries,
        "_debug_sources": playlists,
    }


def _enrich_unknown_vod_dates(
    entries: List[Dict[str, Any]],
    settings: Dict[str, Any],
    *,
    detail_runner: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    cache: Dict[str, Dict[str, Any]],
    exclude_live: bool,
    log_callback: Optional[Callable[[str], None]],
) -> None:
    """Populate one request-local detail cache for valid unknown-date VODs."""
    if not settings.get("enrich_vod_dates", True):
        return

    pending: Dict[str, str] = {}
    for entry in entries:
        if entry_date(entry):
            continue
        if exclude_live and is_live_or_upcoming_entry(entry):
            continue
        vod_id = extract_twitch_vod_id(entry)
        if not vod_id or not vod_id.isdigit() or vod_id in cache:
            continue
        url = canonical_twitch_vod_url(vod_id)
        if url:
            pending.setdefault(vod_id, url)

    if not pending:
        return

    def retrieve(item: tuple[str, str]) -> tuple[str, Dict[str, Any]]:
        vod_id, url = item
        try:
            detail = detail_runner(url, settings)
        except Exception:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        return vod_id, detail

    worker_count = min(VOD_DATE_ENRICHMENT_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        outcomes = executor.map(retrieve, pending.items())
        for vod_id, detail in outcomes:
            cache[vod_id] = detail
            if not entry_date(detail) and log_callback:
                log_callback(
                    f"Date metadata enrichment failed for Twitch VOD {vod_id}."
                )


def search_vods(
    streamers: List[str],
    settings: Dict[str, Any],
    known_vod_ids: set[str],
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
    include_unknown: bool,
    strict_date_filter: bool,
    exclude_live: bool,
    only_real_vods: bool,
    *,
    source_runner: Optional[
        Callable[[str, Any, Optional[Dict[str, Any]]], List[Dict[str, Any]]]
    ] = None,
    detail_runner: Optional[
        Callable[[str, Dict[str, Any]], Dict[str, Any]]
    ] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Search and normalize Twitch VODs without depending on web state."""
    run_sources = source_runner or run_ytdlp_json_sources
    run_detail = detail_runner or run_ytdlp_vod_detail
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    debug: List[Dict[str, Any]] = []
    enrichment_cache: Dict[str, Dict[str, Any]] = {}

    for streamer in streamers:
        try:
            playlists = run_sources(streamer, limit, settings)
            entries: List[Dict[str, Any]] = []
            source_urls: List[str] = []
            seen_entry_keys = set()
            found_raw = 0
            for playlist in playlists:
                if not isinstance(playlist, dict):
                    continue
                source_url = playlist.get("_source_url", "")
                if source_url:
                    source_urls.append(source_url)
                raw_entries = playlist.get("entries") or []
                found_raw += len(raw_entries)
                for raw_entry in raw_entries:
                    if not isinstance(raw_entry, dict):
                        continue
                    entry = dict(raw_entry)
                    raw_url = normalize_vod_url(entry)
                    key = (
                        vod_id_from_url(raw_url)
                        or raw_url
                        or str(entry.get("id") or entry.get("title") or "")
                    )
                    if key and key in seen_entry_keys:
                        continue
                    if key:
                        seen_entry_keys.add(key)
                    entry["_source_url"] = source_url
                    entries.append(entry)
            _enrich_unknown_vod_dates(
                entries,
                settings,
                detail_runner=run_detail,
                cache=enrichment_cache,
                exclude_live=exclude_live,
                log_callback=log_callback,
            )
            kept = 0
            skipped_by_date = 0
            unknown_dates = 0
            date_metadata_enriched = 0
            date_enrichment_failed = 0
            skipped_live = 0
            skipped_nonvod = 0
            for entry in entries:
                url = normalize_vod_url(entry)
                if not url:
                    continue
                if exclude_live and is_live_or_upcoming_entry(entry):
                    skipped_live += 1
                    continue
                if only_real_vods and not is_real_vod_url(url):
                    rescued_url = canonical_twitch_vod_url(entry)
                    if rescued_url:
                        url = rescued_url
                    else:
                        skipped_nonvod += 1
                        continue
                date_str = entry_date(entry)
                enriched = False
                if not date_str and settings.get("enrich_vod_dates", True):
                    vod_id = extract_twitch_vod_id(entry) or vod_id_from_url(url)
                    detail = enrichment_cache.get(vod_id)
                    if detail is not None:
                        if (
                            exclude_live
                            and detail
                            and is_live_or_upcoming_entry(detail)
                        ):
                            skipped_live += 1
                            continue
                        if only_real_vods and detail:
                            detail_url = normalize_vod_url(detail)
                            if detail_url and not is_real_vod_url(detail_url):
                                rescued_url = canonical_twitch_vod_url(detail)
                                if rescued_url:
                                    url = rescued_url
                                else:
                                    skipped_nonvod += 1
                                    continue
                        date_str = entry_date(detail)
                        if date_str:
                            enriched = True
                            date_metadata_enriched += 1
                            if not entry.get("title") and detail.get("title"):
                                entry["title"] = detail.get("title")
                        else:
                            date_enrichment_failed += 1
                if not date_str:
                    unknown_dates += 1
                matches_date = in_range(
                    date_str, start, end, include_unknown
                )
                if not matches_date:
                    skipped_by_date += 1
                    if strict_date_filter or (
                        not date_str and not include_unknown
                    ):
                        continue
                vid = vod_id_from_url(url)
                kept += 1
                results.append(
                    {
                        "streamer": streamer,
                        "title": entry.get("title") or "Untitled",
                        "date": date_str or "unknown",
                        "url": url,
                        "id": vid,
                        "already_downloaded": vid in known_vod_ids,
                        "date_enriched": enriched,
                        "outside_range": not matches_date,
                    }
                )
            debug.append(
                {
                    "streamer": streamer,
                    "source": ", ".join(source_urls),
                    "found_raw": found_raw,
                    "deduped": len(entries),
                    "kept": kept,
                    "unknown_dates": unknown_dates,
                    "date_metadata_enriched": date_metadata_enriched,
                    "date_enrichment_failed": date_enrichment_failed,
                    "skipped_by_date": skipped_by_date,
                    "skipped_live": skipped_live,
                    "skipped_nonvod": skipped_nonvod,
                }
            )
        except FileNotFoundError:
            errors.append(
                {
                    "streamer": streamer,
                    "error": "The yt-dlp Python module was not found. Install the dependencies from requirements.txt.",
                }
            )
        except Exception as exc:
            errors.append({"streamer": streamer, "error": str(exc)})

    def sort_key(item: Dict[str, Any]) -> tuple[str, str, str]:
        date_value = (
            item.get("date")
            if item.get("date") != "unknown"
            else "0000-00-00"
        )
        return (
            str(date_value),
            str(item.get("streamer", "")),
            str(item.get("title", "")),
        )

    results.sort(key=sort_key, reverse=True)
    return {"results": results, "errors": errors, "debug": debug}
