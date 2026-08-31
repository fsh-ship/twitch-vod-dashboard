"""Small, local cache for public Twitch streamer profile images."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from vod_dashboard.runtime_files import atomic_write_bytes, atomic_write_text
from vod_dashboard.settings import canonical_streamer_login


TWITCH_USERS_ENDPOINT = "https://api.twitch.tv/helix/users"
PROFILE_TTL = timedelta(hours=24)
INDEX_FILENAME = "index.json"
MAX_HELIX_USERS = 100
MAX_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def canonical_login(value: Any) -> str:
    """Apply the same canonical Twitch-login rules as streamer settings."""
    return canonical_streamer_login(value)


def normalize_logins(values: Iterable[Any]) -> list[str]:
    """Return unique canonical logins while retaining configured order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        login = canonical_login(value)
        if login and login not in seen:
            normalized.append(login)
            seen.add(login)
    return normalized


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def metadata_is_fresh(
    entry: Mapping[str, Any], now: Optional[datetime] = None
) -> bool:
    try:
        fetched_at = datetime.fromisoformat(
            str(entry.get("fetched_at") or "").replace("Z", "+00:00")
        )
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return fetched_at + PROFILE_TTL > (now or _utc_now())
    except (TypeError, ValueError, OverflowError):
        return False


def _clean_entry(raw_login: Any, raw_entry: Any) -> Optional[dict[str, str]]:
    login = canonical_login(raw_login)
    if not login or not isinstance(raw_entry, Mapping):
        return None
    entry_login = canonical_login(raw_entry.get("login") or login)
    if entry_login != login:
        return None
    filename = str(raw_entry.get("filename") or "").strip()
    if filename and (
        Path(filename).name != filename or "/" in filename or "\\" in filename
    ):
        filename = ""
    content_type = str(raw_entry.get("content_type") or "").split(";", 1)[0].lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        content_type = ""
    return {
        "login": login,
        "display_name": str(raw_entry.get("display_name") or login),
        "profile_image_url": str(raw_entry.get("profile_image_url") or ""),
        "filename": filename,
        "content_type": content_type,
        "fetched_at": str(raw_entry.get("fetched_at") or ""),
    }


def load_metadata(path: Path) -> dict[str, dict[str, str]]:
    """Load a cache index; a missing or malformed file is an empty cache."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    raw_profiles = document.get("profiles") if isinstance(document, Mapping) else None
    if not isinstance(raw_profiles, Mapping):
        return {}
    profiles: dict[str, dict[str, str]] = {}
    for raw_login, raw_entry in raw_profiles.items():
        entry = _clean_entry(raw_login, raw_entry)
        if entry is not None and entry["login"] not in profiles:
            profiles[entry["login"]] = entry
    return profiles


def save_metadata(path: Path, profiles: Mapping[str, Mapping[str, Any]]) -> None:
    clean_profiles: dict[str, dict[str, str]] = {}
    for raw_login, raw_entry in profiles.items():
        entry = _clean_entry(raw_login, raw_entry)
        if entry is not None:
            clean_profiles[entry["login"]] = entry
    atomic_write_text(
        Path(path),
        json.dumps({"profiles": clean_profiles}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def twitch_users_lookup(
    logins: Iterable[Any],
    client_id: str,
    access_token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, str]]:
    """Fetch public profile fields from Helix, in batches of at most 100."""
    normalized = normalize_logins(logins)
    if not normalized or not str(client_id).strip() or not str(access_token).strip():
        return []

    users: list[dict[str, str]] = []
    for offset in range(0, len(normalized), MAX_HELIX_USERS):
        batch = normalized[offset : offset + MAX_HELIX_USERS]
        query = urlencode([("login", login) for login in batch])
        request = Request(
            f"{TWITCH_USERS_ENDPOINT}?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {str(access_token).strip()}",
                "Client-Id": str(client_id).strip(),
                "User-Agent": "Twitch-VOD-Dashboard",
            },
        )
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw_users = payload.get("data") if isinstance(payload, Mapping) else None
        for raw_user in raw_users if isinstance(raw_users, list) else []:
            if not isinstance(raw_user, Mapping):
                continue
            login = canonical_login(raw_user.get("login"))
            if not login or login not in batch:
                continue
            users.append(
                {
                    "login": login,
                    "display_name": str(raw_user.get("display_name") or login),
                    "profile_image_url": str(raw_user.get("profile_image_url") or ""),
                }
            )
    return users


def download_avatar(
    image_url: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> tuple[bytes, str, str]:
    """Download one bounded image and return bytes, MIME type, and extension."""
    parsed = urlsplit(str(image_url or ""))
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("Twitch profile images must use HTTPS.")
    request = Request(
        image_url,
        headers={"Accept": "image/*", "User-Agent": "Twitch-VOD-Dashboard"},
    )
    with opener(request, timeout=20) as response:
        content_type = str(response.headers.get_content_type()).lower()
        extension = ALLOWED_IMAGE_TYPES.get(content_type)
        if not extension:
            raise ValueError("Unsupported Twitch profile-image content type.")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_AVATAR_BYTES:
            raise ValueError("Twitch profile image is too large.")
        content = response.read(MAX_AVATAR_BYTES + 1)
    if not content or len(content) > MAX_AVATAR_BYTES:
        raise ValueError("Twitch profile image is empty or too large.")
    return content, content_type, extension


class StreamerProfileCache:
    """Refresh and serve a small persistent cache of configured streamers."""

    def __init__(
        self,
        directory: Path,
        *,
        user_lookup: Callable[
            [Iterable[Any], str, str], list[dict[str, str]]
        ] = twitch_users_lookup,
        image_downloader: Callable[[str], tuple[bytes, str, str]] = download_avatar,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.directory = Path(directory)
        self.index_path = self.directory / INDEX_FILENAME
        self.user_lookup = user_lookup
        self.image_downloader = image_downloader
        self.clock = clock
        self._lock = threading.Lock()

    def _avatar_path(self, entry: Mapping[str, Any]) -> Optional[Path]:
        filename = str(entry.get("filename") or "")
        if not filename or Path(filename).name != filename:
            return None
        base = self.directory.resolve()
        candidate = (self.directory / filename).resolve()
        if candidate.parent != base or not candidate.is_file():
            return None
        return candidate

    def _store_image(self, login: str, image_url: str) -> tuple[str, str]:
        content, content_type, extension = self.image_downloader(image_url)
        filename = f"{login}{extension}"
        atomic_write_bytes(self.directory / filename, content)
        return filename, content_type

    def refresh(
        self,
        logins: Iterable[Any],
        *,
        client_id: str = "",
        access_token: str = "",
    ) -> dict[str, dict[str, str]]:
        """Refresh stale configured profiles and return the complete cache index."""
        requested = normalize_logins(logins)
        with self._lock:
            profiles = load_metadata(self.index_path)
            changed = False
            now = self.clock()

            # A missing local file can be repaired from its cached URL without a
            # Helix lookup, including when credentials are temporarily unavailable.
            for login in requested:
                entry = profiles.get(login)
                if not entry or self._avatar_path(entry) is not None:
                    continue
                image_url = str(entry.get("profile_image_url") or "")
                if not image_url:
                    continue
                try:
                    filename, content_type = self._store_image(login, image_url)
                except Exception:
                    continue
                entry["filename"] = filename
                entry["content_type"] = content_type
                profiles[login] = entry
                changed = True

            stale = [
                login
                for login in requested
                if not metadata_is_fresh(profiles.get(login, {}), now)
            ]
            if stale and str(client_id).strip() and str(access_token).strip():
                try:
                    fetched_users = self.user_lookup(stale, client_id, access_token)
                except Exception:
                    fetched_users = None
                if fetched_users is not None:
                    fetched_by_login = {
                        user_login: user
                        for user in fetched_users
                        if (user_login := canonical_login(user.get("login"))) in stale
                    }
                    for login in stale:
                        previous = profiles.get(login)
                        user = fetched_by_login.get(login)
                        if user is None:
                            if previous is None:
                                profiles[login] = {
                                    "login": login,
                                    "display_name": login,
                                    "profile_image_url": "",
                                    "filename": "",
                                    "content_type": "",
                                    "fetched_at": _timestamp(now),
                                }
                            else:
                                previous["fetched_at"] = _timestamp(now)
                            changed = True
                            continue

                        image_url = str(user.get("profile_image_url") or "")
                        unchanged_image = bool(
                            previous
                            and image_url
                            and image_url == previous.get("profile_image_url")
                            and self._avatar_path(previous) is not None
                        )
                        if unchanged_image:
                            filename = str(previous.get("filename") or "")
                            content_type = str(previous.get("content_type") or "")
                        elif image_url:
                            try:
                                filename, content_type = self._store_image(login, image_url)
                            except Exception:
                                if previous is not None:
                                    previous["fetched_at"] = _timestamp(now)
                                changed = True
                                continue
                        else:
                            if previous is not None:
                                previous["fetched_at"] = _timestamp(now)
                            changed = True
                            continue

                        profiles[login] = {
                            "login": login,
                            "display_name": str(user.get("display_name") or login),
                            "profile_image_url": image_url,
                            "filename": filename,
                            "content_type": content_type,
                            "fetched_at": _timestamp(now),
                        }
                        changed = True

            if changed:
                save_metadata(self.index_path, profiles)
            return profiles

    def public_profiles(
        self,
        logins: Iterable[Any],
        *,
        client_id: str = "",
        access_token: str = "",
    ) -> dict[str, dict[str, str]]:
        profiles = self.refresh(
            logins, client_id=client_id, access_token=access_token
        )
        public: dict[str, dict[str, str]] = {}
        for login in normalize_logins(logins):
            entry = profiles.get(login)
            if entry is None or self._avatar_path(entry) is None:
                continue
            public[login] = {
                "login": login,
                "display_name": str(entry.get("display_name") or login),
                "avatar_url": f"/api/streamer-avatar/{login}",
            }
        return public

    def resolve_avatar(self, raw_login: Any) -> Optional[tuple[Path, str]]:
        login = canonical_login(raw_login)
        if not login:
            return None
        entry = load_metadata(self.index_path).get(login)
        if entry is None:
            return None
        path = self._avatar_path(entry)
        content_type = str(entry.get("content_type") or "")
        if path is None or content_type not in ALLOWED_IMAGE_TYPES:
            return None
        return path, content_type
