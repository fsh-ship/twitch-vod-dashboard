"""Framework-independent authentication configuration and login throttling."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import ipaddress
import os
import secrets
import threading
import time
from typing import Any, Callable, Mapping, MutableMapping, Optional
from urllib.parse import urlsplit

from werkzeug.security import check_password_hash


LOGIN_ATTEMPT_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def canonical_origin(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def generate_csrf_token(
    token_factory: Optional[Callable[[int], str]] = None,
) -> str:
    factory = token_factory or secrets.token_urlsafe
    return factory(32)


def csrf_token_matches(
    expected: Any,
    supplied: Any,
    comparator: Optional[Callable[[str, str], bool]] = None,
) -> bool:
    expected_token = str(expected or "")
    supplied_token = str(supplied or "")
    compare = comparator or secrets.compare_digest
    return bool(
        expected_token
        and supplied_token
        and compare(expected_token, supplied_token)
    )


def method_requires_csrf(method: Any) -> bool:
    return method in STATE_CHANGING_METHODS


def origin_is_allowed(
    raw_origin: Any,
    raw_referer: Any,
    configured_origins: Any,
    host_url: Any,
    origin_normalizer: Optional[Callable[[str], str]] = None,
) -> bool:
    if not raw_origin and not raw_referer:
        return True

    normalize_origin = origin_normalizer or canonical_origin
    supplied = normalize_origin(raw_origin or raw_referer or "")
    if not supplied:
        return False
    configured = set(configured_origins or ())
    allowed = configured or {normalize_origin(host_url)}
    return supplied in allowed


def host_is_allowed(request_host: Any, trusted_hosts: Any) -> bool:
    trusted = tuple(trusted_hosts or ())
    if not trusted:
        return True

    def parse_host(value: Any) -> Optional[tuple[str, Optional[int]]]:
        raw = str(value or "").strip().lower()
        if (
            not raw
            or raw.endswith(":")
            or any(character.isspace() for character in raw)
            or any(character in raw for character in "/?#@,\\")
        ):
            return None
        try:
            parsed = urlsplit(f"//{raw}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if ":" in hostname and not raw.startswith("["):
            return None
        try:
            normalized_hostname = str(ipaddress.ip_address(hostname))
        except ValueError:
            normalized_hostname = hostname
        return normalized_hostname, port

    supplied = parse_host(request_host)
    if supplied is None:
        return False
    supplied_hostname, supplied_port = supplied
    for trusted_host in trusted:
        configured = parse_host(trusted_host)
        if configured is None:
            continue
        configured_hostname, configured_port = configured
        if configured_hostname != supplied_hostname:
            continue
        if configured_port is None or configured_port == supplied_port:
            return True
    return False


@dataclass(frozen=True)
class SecurityConfig:
    username: str
    password_hash: str
    secret_key: str
    auth_disabled: bool
    allowed_origins: tuple[str, ...]
    trusted_hosts: tuple[str, ...]
    session_cookie_secure: bool
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    session_cookie_name: str = "vod_dashboard_session"
    permanent_session_lifetime: timedelta = timedelta(hours=12)

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        origin_normalizer: Optional[Callable[[str], str]] = None,
    ) -> "SecurityConfig":
        env = os.environ if environ is None else environ
        normalize_origin = origin_normalizer or canonical_origin
        disabled = str(env.get("VOD_DASHBOARD_AUTH_DISABLED") or "").strip() == "1"
        username = str(env.get("VOD_DASHBOARD_USERNAME") or "").strip()
        password_hash = str(
            env.get("VOD_DASHBOARD_PASSWORD_HASH") or ""
        ).strip()
        secret_key = str(env.get("VOD_DASHBOARD_SECRET_KEY") or "").strip()

        if not disabled:
            missing = [
                name
                for name, value in (
                    ("VOD_DASHBOARD_USERNAME", username),
                    ("VOD_DASHBOARD_PASSWORD_HASH", password_hash),
                    ("VOD_DASHBOARD_SECRET_KEY", secret_key),
                )
                if not value
            ]
            if missing:
                raise RuntimeError(
                    "Authentication is enabled by default. Missing environment variables: "
                    + ", ".join(missing)
                    + ". Set VOD_DASHBOARD_AUTH_DISABLED=1 only for local development."
                )
            if password_hash.count("$") < 2:
                raise RuntimeError(
                    "VOD_DASHBOARD_PASSWORD_HASH is not a Werkzeug-compatible password hash."
                )
            try:
                check_password_hash(password_hash, secrets.token_urlsafe(12))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "VOD_DASHBOARD_PASSWORD_HASH is not a Werkzeug-compatible password hash."
                ) from exc
        elif not secret_key:
            secret_key = secrets.token_hex(32)

        if len(secret_key) < 32:
            raise RuntimeError(
                "VOD_DASHBOARD_SECRET_KEY must contain at least 32 characters."
            )

        cookie_secure_raw = str(
            env.get("VOD_DASHBOARD_SESSION_COOKIE_SECURE") or "0"
        ).strip()
        if cookie_secure_raw not in {"0", "1"}:
            raise RuntimeError(
                "VOD_DASHBOARD_SESSION_COOKIE_SECURE must be 0 or 1."
            )

        allowed_origins = tuple(
            origin
            for origin in (
                normalize_origin(item)
                for item in str(
                    env.get("VOD_DASHBOARD_ALLOWED_ORIGINS") or ""
                ).split(",")
            )
            if origin
        )
        trusted_hosts = tuple(
            item.strip().lower()
            for item in str(
                env.get("VOD_DASHBOARD_TRUSTED_HOSTS") or ""
            ).split(",")
            if item.strip()
        )
        return cls(
            username=username,
            password_hash=password_hash,
            secret_key=secret_key,
            auth_disabled=disabled,
            allowed_origins=allowed_origins,
            trusted_hosts=trusted_hosts,
            session_cookie_secure=cookie_secure_raw == "1",
        )

    def as_flask_config(self) -> dict[str, Any]:
        return {
            "VOD_AUTH_DISABLED": self.auth_disabled,
            "VOD_USERNAME": self.username,
            "VOD_PASSWORD_HASH": self.password_hash,
            "VOD_ALLOWED_ORIGINS": self.allowed_origins,
            "VOD_TRUSTED_HOSTS": self.trusted_hosts,
            "SECRET_KEY": self.secret_key,
            "SESSION_COOKIE_HTTPONLY": self.session_cookie_httponly,
            "SESSION_COOKIE_SAMESITE": self.session_cookie_samesite,
            "SESSION_COOKIE_SECURE": self.session_cookie_secure,
            "SESSION_COOKIE_NAME": self.session_cookie_name,
            "PERMANENT_SESSION_LIFETIME": self.permanent_session_lifetime,
        }


def security_config_from_environment(
    environ: Optional[Mapping[str, str]] = None,
    origin_normalizer: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    return SecurityConfig.from_environment(
        environ, origin_normalizer=origin_normalizer
    ).as_flask_config()


def login_attempt_key(remote_addr: Any) -> str:
    return str(remote_addr or "unknown")


def username_matches(candidate: str, expected: str) -> bool:
    candidate_digest = hashlib.sha256(
        candidate.encode("utf-8", errors="replace")
    ).digest()
    expected_digest = hashlib.sha256(
        expected.encode("utf-8", errors="replace")
    ).digest()
    return secrets.compare_digest(candidate_digest, expected_digest)


def password_matches(password_hash: str, candidate: str) -> bool:
    return check_password_hash(password_hash, candidate)


class LoginThrottle:
    """Thread-safe, process-local failed-login tracking."""

    def __init__(
        self,
        attempts: Optional[MutableMapping[str, deque[float]]] = None,
        lock: Optional[threading.Lock] = None,
        window_seconds: int = LOGIN_ATTEMPT_WINDOW_SECONDS,
        max_failures: int = LOGIN_MAX_FAILURES,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.attempts = attempts if attempts is not None else defaultdict(deque)
        self.lock = lock if lock is not None else threading.Lock()
        self.window_seconds = window_seconds
        self.max_failures = max_failures
        self.clock = clock or time.monotonic

    def retry_after(self, key: str) -> int:
        now = self.clock()
        cutoff = now - self.window_seconds
        with self.lock:
            attempts = self.attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if not attempts:
                self.attempts.pop(key, None)
                return 0
            if len(attempts) < self.max_failures:
                return 0
            return max(1, int(self.window_seconds - (now - attempts[0])))

    def record_failure(self, key: str) -> None:
        now = self.clock()
        cutoff = now - self.window_seconds
        with self.lock:
            attempts = self.attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            attempts.append(now)
            if len(self.attempts) > 1000:
                stale_keys = [
                    item_key
                    for item_key, values in self.attempts.items()
                    if not values or values[-1] < cutoff
                ]
                for item_key in stale_keys:
                    self.attempts.pop(item_key, None)

    def clear_failures(self, key: str) -> None:
        with self.lock:
            self.attempts.pop(key, None)
