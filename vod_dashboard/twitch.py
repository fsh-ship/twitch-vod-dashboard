from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
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
    for key in ("upload_date", "release_date", "timestamp"):
        val = entry.get(key)
        if not val:
            continue
        if key == "timestamp":
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
        command, capture_output=True, text=True, timeout=180
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
            kept = 0
            skipped_by_date = 0
            unknown_dates = 0
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
                    try:
                        detail = run_detail(url, settings)
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
                                    detail_url = rescued_url
                                    url = rescued_url
                                else:
                                    skipped_nonvod += 1
                                    continue
                        date_str = entry_date(detail)
                        if date_str:
                            enriched = True
                            if not entry.get("title") and detail.get("title"):
                                entry["title"] = detail.get("title")
                    except Exception as exc:
                        if log_callback:
                            log_callback(
                                f"Could not retrieve the date for {url}: {exc}"
                            )
                if not date_str:
                    unknown_dates += 1
                matches_date = in_range(
                    date_str, start, end, include_unknown
                )
                if not matches_date:
                    skipped_by_date += 1
                    if strict_date_filter:
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
