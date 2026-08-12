import os
import sys
import tempfile
import threading
import unittest
from collections import defaultdict, deque
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from werkzeug.security import generate_password_hash

from vod_dashboard import security


class MutableClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class SecurityConfigTests(unittest.TestCase):
    USERNAME = "administrator"
    PASSWORD = "correct horse battery staple"
    SECRET_KEY = "s" * 32

    def valid_environment(self, **updates):
        environ = {
            "VOD_DASHBOARD_USERNAME": self.USERNAME,
            "VOD_DASHBOARD_PASSWORD_HASH": generate_password_hash(self.PASSWORD),
            "VOD_DASHBOARD_SECRET_KEY": self.SECRET_KEY,
        }
        environ.update(updates)
        return environ

    def test_complete_valid_security_configuration(self):
        config = security.SecurityConfig.from_environment(self.valid_environment())

        self.assertEqual(config.username, self.USERNAME)
        self.assertFalse(config.auth_disabled)
        self.assertEqual(config.secret_key, self.SECRET_KEY)
        self.assertFalse(config.session_cookie_secure)
        self.assertEqual(
            config.as_flask_config(),
            {
                "VOD_AUTH_DISABLED": False,
                "VOD_USERNAME": self.USERNAME,
                "VOD_PASSWORD_HASH": config.password_hash,
                "VOD_ALLOWED_ORIGINS": (),
                "VOD_TRUSTED_HOSTS": (),
                "SECRET_KEY": self.SECRET_KEY,
                "SESSION_COOKIE_HTTPONLY": True,
                "SESSION_COOKIE_SAMESITE": "Lax",
                "SESSION_COOKIE_SECURE": False,
                "SESSION_COOKIE_NAME": "vod_dashboard_session",
                "PERMANENT_SESSION_LIFETIME": security.timedelta(hours=12),
            },
        )
        with self.assertRaises(FrozenInstanceError):
            config.username = "changed"

    def test_missing_username_is_rejected(self):
        environ = self.valid_environment()
        environ.pop("VOD_DASHBOARD_USERNAME")
        with self.assertRaisesRegex(RuntimeError, "VOD_DASHBOARD_USERNAME"):
            security.SecurityConfig.from_environment(environ)

    def test_missing_password_hash_is_rejected(self):
        environ = self.valid_environment()
        environ.pop("VOD_DASHBOARD_PASSWORD_HASH")
        with self.assertRaisesRegex(RuntimeError, "VOD_DASHBOARD_PASSWORD_HASH"):
            security.SecurityConfig.from_environment(environ)

    def test_missing_secret_key_is_rejected(self):
        environ = self.valid_environment()
        environ.pop("VOD_DASHBOARD_SECRET_KEY")
        with self.assertRaisesRegex(RuntimeError, "VOD_DASHBOARD_SECRET_KEY"):
            security.SecurityConfig.from_environment(environ)

    def test_too_short_secret_key_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
            security.SecurityConfig.from_environment(
                self.valid_environment(VOD_DASHBOARD_SECRET_KEY="too-short")
            )

    def test_invalid_password_hash_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Werkzeug-compatible"):
            security.SecurityConfig.from_environment(
                self.valid_environment(VOD_DASHBOARD_PASSWORD_HASH="plaintext")
            )

    def test_auth_disabled_mode_generates_only_the_missing_session_secret(self):
        with mock.patch.object(
            security.secrets, "token_hex", return_value="d" * 64
        ) as token_hex:
            config = security.SecurityConfig.from_environment(
                {"VOD_DASHBOARD_AUTH_DISABLED": "1"}
            )

        self.assertTrue(config.auth_disabled)
        self.assertEqual(config.username, "")
        self.assertEqual(config.password_hash, "")
        self.assertEqual(config.secret_key, "d" * 64)
        token_hex.assert_called_once_with(32)

    def test_allowed_origins_and_trusted_hosts_are_parsed_exactly(self):
        config = security.SecurityConfig.from_environment(
            self.valid_environment(
                VOD_DASHBOARD_ALLOWED_ORIGINS=(
                    " HTTPS://Example.COM/path,invalid, http://LOCALHOST:8080 "
                ),
                VOD_DASHBOARD_TRUSTED_HOSTS=" Dashboard.EXAMPLE, localhost:8080 ",
            )
        )

        self.assertEqual(
            config.allowed_origins,
            ("https://example.com", "http://localhost:8080"),
        )
        self.assertEqual(
            config.trusted_hosts, ("dashboard.example", "localhost:8080")
        )

    def test_secure_cookie_parsing_accepts_only_zero_or_one(self):
        secure = security.SecurityConfig.from_environment(
            self.valid_environment(VOD_DASHBOARD_SESSION_COOKIE_SECURE="1")
        )
        insecure = security.SecurityConfig.from_environment(
            self.valid_environment(VOD_DASHBOARD_SESSION_COOKIE_SECURE="0")
        )
        self.assertTrue(secure.session_cookie_secure)
        self.assertFalse(insecure.session_cookie_secure)

        with self.assertRaisesRegex(RuntimeError, "must be 0 or 1"):
            security.SecurityConfig.from_environment(
                self.valid_environment(VOD_DASHBOARD_SESSION_COOKIE_SECURE="true")
            )


class CredentialHelperTests(unittest.TestCase):
    def test_canonical_origin_and_login_attempt_key_contracts(self):
        self.assertEqual(
            security.canonical_origin(" HTTPS://Example.COM/a/path "),
            "https://example.com",
        )
        self.assertEqual(security.canonical_origin("ftp://example.com"), "")
        self.assertEqual(security.login_attempt_key("127.0.0.1"), "127.0.0.1")
        self.assertEqual(security.login_attempt_key(None), "unknown")

    def test_valid_and_invalid_usernames(self):
        self.assertTrue(security.username_matches("admin", "admin"))
        self.assertFalse(security.username_matches("Admin", "admin"))
        self.assertFalse(security.username_matches("admin ", "admin"))

    def test_valid_and_invalid_password_verification(self):
        password_hash = generate_password_hash("correct password")
        self.assertTrue(security.password_matches(password_hash, "correct password"))
        self.assertFalse(security.password_matches(password_hash, "wrong password"))


class CsrfPolicyTests(unittest.TestCase):
    def test_matching_and_mismatching_tokens(self):
        self.assertTrue(security.csrf_token_matches("token", "token"))
        self.assertFalse(security.csrf_token_matches("token", "different"))

    def test_missing_expected_or_supplied_token_is_invalid(self):
        self.assertFalse(security.csrf_token_matches("", "token"))
        self.assertFalse(security.csrf_token_matches(None, "token"))
        self.assertFalse(security.csrf_token_matches("token", ""))
        self.assertFalse(security.csrf_token_matches("token", None))

    def test_nonempty_tokens_use_the_supplied_constant_time_comparator(self):
        comparator = mock.Mock(return_value=True)
        self.assertTrue(
            security.csrf_token_matches("expected", "supplied", comparator)
        )
        comparator.assert_called_once_with("expected", "supplied")

        comparator.reset_mock()
        self.assertFalse(security.csrf_token_matches("", "supplied", comparator))
        comparator.assert_not_called()

    def test_token_generation_preserves_32_byte_urlsafe_request(self):
        token_factory = mock.Mock(return_value="generated-token")
        self.assertEqual(
            security.generate_csrf_token(token_factory), "generated-token"
        )
        token_factory.assert_called_once_with(32)

    def test_only_existing_state_changing_methods_require_csrf(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertTrue(security.method_requires_csrf(method), method)
        for method in ("GET", "HEAD", "OPTIONS", "post", ""):
            self.assertFalse(security.method_requires_csrf(method), method)


class OriginPolicyTests(unittest.TestCase):
    def test_origin_matching_current_host_is_allowed_without_configuration(self):
        self.assertTrue(
            security.origin_is_allowed(
                "http://localhost", None, (), "http://localhost/"
            )
        )

    def test_disallowed_and_malformed_origins_are_rejected(self):
        self.assertFalse(
            security.origin_is_allowed(
                "https://attacker.invalid", None, (), "http://localhost/"
            )
        )
        self.assertFalse(
            security.origin_is_allowed("not-an-origin", None, (), "http://localhost/")
        )

    def test_missing_origin_and_referer_remains_allowed(self):
        normalizer = mock.Mock(side_effect=AssertionError("must not normalize"))
        self.assertTrue(
            security.origin_is_allowed(
                None, None, ("https://configured.example",), "bad", normalizer
            )
        )
        normalizer.assert_not_called()

    def test_origin_port_must_match_exactly(self):
        self.assertTrue(
            security.origin_is_allowed(
                "http://localhost:8080", None, (), "http://localhost:8080/"
            )
        )
        self.assertFalse(
            security.origin_is_allowed(
                "http://localhost:8081", None, (), "http://localhost:8080/"
            )
        )

    def test_multiple_configured_origins_replace_implicit_host_origin(self):
        configured = (
            "https://one.example",
            "https://two.example:8443",
        )
        self.assertTrue(
            security.origin_is_allowed(
                "https://two.example:8443", None, configured, "http://localhost/"
            )
        )
        self.assertFalse(
            security.origin_is_allowed(
                "http://localhost", None, configured, "http://localhost/"
            )
        )

    def test_referer_path_is_canonicalized_when_origin_is_missing(self):
        self.assertTrue(
            security.origin_is_allowed(
                None,
                "https://example.test/a/form?value=1",
                ("https://example.test",),
                "http://localhost/",
            )
        )

    def test_present_malformed_origin_takes_precedence_over_valid_referer(self):
        self.assertFalse(
            security.origin_is_allowed(
                "malformed",
                "https://example.test/path",
                ("https://example.test",),
                "http://localhost/",
            )
        )


class HostPolicyTests(unittest.TestCase):
    def test_empty_trusted_host_configuration_allows_any_host(self):
        self.assertTrue(security.host_is_allowed("example.test", ()))
        self.assertTrue(security.host_is_allowed("", ()))

    def test_bare_trusted_hostname_accepts_host_with_any_port(self):
        self.assertTrue(
            security.host_is_allowed("Dashboard.Example", ("dashboard.example",))
        )
        self.assertTrue(
            security.host_is_allowed("Dashboard.Example:8080", ("dashboard.example",))
        )

    def test_trusted_host_with_port_requires_that_exact_port(self):
        trusted = ("dashboard.example:8443",)
        self.assertTrue(
            security.host_is_allowed("DASHBOARD.EXAMPLE:8443", trusted)
        )
        self.assertFalse(security.host_is_allowed("dashboard.example:8080", trusted))
        self.assertFalse(security.host_is_allowed("dashboard.example", trusted))

    def test_untrusted_malformed_and_missing_hosts_are_rejected(self):
        trusted = ("dashboard.example",)
        self.assertFalse(security.host_is_allowed("attacker.example", trusted))
        self.assertFalse(security.host_is_allowed(":malformed", trusted))
        self.assertFalse(security.host_is_allowed("", trusted))
        self.assertFalse(security.host_is_allowed(None, trusted))

    def test_multiple_trusted_hosts_and_request_case_handling(self):
        trusted = ("one.example", "two.example:8443")
        self.assertTrue(security.host_is_allowed("ONE.EXAMPLE", trusted))
        self.assertTrue(security.host_is_allowed("TWO.EXAMPLE:8443", trusted))

    def test_ipv4_bare_and_explicit_port_matching(self):
        self.assertTrue(security.host_is_allowed("127.0.0.1", ("127.0.0.1",)))
        self.assertTrue(
            security.host_is_allowed("127.0.0.1:8787", ("127.0.0.1",))
        )
        self.assertTrue(
            security.host_is_allowed("127.0.0.1:8787", ("127.0.0.1:8787",))
        )
        self.assertFalse(
            security.host_is_allowed("127.0.0.1:8788", ("127.0.0.1:8787",))
        )

    def test_bare_bracketed_ipv6_accepts_no_port_or_any_port(self):
        self.assertTrue(security.host_is_allowed("[::1]", ("[::1]",)))
        self.assertTrue(
            security.host_is_allowed("[::1]:5000", ("[::1]",))
        )

    def test_explicit_trusted_ipv6_port_requires_exact_port(self):
        trusted = ("[::1]:5000",)
        self.assertTrue(security.host_is_allowed("[::1]:5000", trusted))
        self.assertFalse(security.host_is_allowed("[::1]:5001", trusted))
        self.assertFalse(security.host_is_allowed("[::1]", trusted))

    def test_equivalent_ipv6_text_is_normalized_before_matching(self):
        self.assertTrue(
            security.host_is_allowed(
                "[0:0:0:0:0:0:0:1]:8787", ("[::1]",)
            )
        )

    def test_untrusted_and_malformed_ipv6_hosts_are_rejected(self):
        self.assertFalse(
            security.host_is_allowed("[::2]:5000", ("[::1]",))
        )
        for malformed in (
            "::1",
            "[::1",
            "[::1]:",
            "[::1]:not-a-port",
            "[::1]:70000",
            "[::1]/path",
            "[::1]\r\nX-Header: value",
        ):
            with self.subTest(malformed=malformed):
                self.assertFalse(
                    security.host_is_allowed(malformed, ("[::1]",))
                )


class LoginThrottleTests(unittest.TestCase):
    def throttle(self, clock=None):
        return security.LoginThrottle(clock=clock or MutableClock())

    def test_first_failed_attempt_is_recorded_without_throttling(self):
        throttle = self.throttle()
        throttle.record_failure("client")

        self.assertEqual(list(throttle.attempts["client"]), [100.0])
        self.assertEqual(throttle.retry_after("client"), 0)

    def test_fifth_failed_attempt_starts_five_minute_throttle(self):
        throttle = self.throttle()
        for _ in range(5):
            throttle.record_failure("client")

        self.assertEqual(throttle.retry_after("client"), 300)

    def test_retry_after_uses_existing_integer_timing_behavior(self):
        clock = MutableClock(100.0)
        throttle = self.throttle(clock)
        for _ in range(5):
            throttle.record_failure("client")
        clock.value = 130.5

        self.assertEqual(throttle.retry_after("client"), 269)

    def test_attempts_expire_after_the_five_minute_window(self):
        clock = MutableClock(100.0)
        throttle = self.throttle(clock)
        for _ in range(5):
            throttle.record_failure("client")
        clock.value = 400.001

        self.assertEqual(throttle.retry_after("client"), 0)
        self.assertNotIn("client", throttle.attempts)

    def test_successful_login_clearing_removes_failures(self):
        throttle = self.throttle()
        throttle.record_failure("client")
        throttle.clear_failures("client")

        self.assertNotIn("client", throttle.attempts)
        self.assertEqual(throttle.retry_after("client"), 0)

    def test_concurrent_failure_recording_is_thread_safe(self):
        throttle = self.throttle()
        threads = [
            threading.Thread(target=throttle.record_failure, args=("client",))
            for _ in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(throttle.attempts["client"]), 40)
        self.assertEqual(throttle.retry_after("client"), 300)


_IMPORT_TMP = None
_OLD_ENV = {}
if "app" not in sys.modules:
    _IMPORT_TMP = tempfile.TemporaryDirectory()
    import_base = Path(_IMPORT_TMP.name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
        "VOD_DASHBOARD_LEGACY_SETTINGS_PATH",
    ):
        _OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(import_base / "data" / "settings.json")
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)

import app as dashboard  # noqa: E402


def tearDownModule():
    if _IMPORT_TMP is not None:
        _IMPORT_TMP.cleanup()
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AppSecurityCompatibilityTests(unittest.TestCase):
    def test_configuration_and_state_aliases_remain_available(self):
        self.assertIs(dashboard.SecurityConfig, security.SecurityConfig)
        self.assertIs(dashboard.canonical_origin, security.canonical_origin)
        self.assertIs(dashboard.login_attempts, dashboard.LOGIN_THROTTLE.attempts)
        self.assertIs(dashboard.login_attempt_lock, dashboard.LOGIN_THROTTLE.lock)
        self.assertEqual(dashboard.LOGIN_MAX_FAILURES, 5)
        self.assertEqual(dashboard.LOGIN_ATTEMPT_WINDOW_SECONDS, 300)

    def test_app_configuration_wrapper_matches_security_module(self):
        environ = {
            "VOD_DASHBOARD_AUTH_DISABLED": "1",
            "VOD_DASHBOARD_SECRET_KEY": "x" * 32,
            "VOD_DASHBOARD_SESSION_COOKIE_SECURE": "1",
        }
        self.assertEqual(
            dashboard.security_config_from_environment(environ),
            security.security_config_from_environment(environ),
        )

    def test_app_configuration_wrapper_honors_patched_origin_helper(self):
        environ = {
            "VOD_DASHBOARD_AUTH_DISABLED": "1",
            "VOD_DASHBOARD_SECRET_KEY": "x" * 32,
            "VOD_DASHBOARD_ALLOWED_ORIGINS": "custom-origin",
        }
        with mock.patch.object(
            dashboard, "canonical_origin", return_value="https://patched.example"
        ) as normalizer:
            config = dashboard.security_config_from_environment(environ)

        self.assertEqual(
            config["VOD_ALLOWED_ORIGINS"], ("https://patched.example",)
        )
        normalizer.assert_called_once_with("custom-origin")

    def test_app_throttle_wrappers_honor_patched_state_constants_and_clock(self):
        attempts = defaultdict(deque)
        lock = threading.Lock()
        with (
            mock.patch.object(dashboard, "login_attempts", attempts),
            mock.patch.object(dashboard, "login_attempt_lock", lock),
            mock.patch.object(dashboard, "LOGIN_MAX_FAILURES", 2),
            mock.patch.object(dashboard, "LOGIN_ATTEMPT_WINDOW_SECONDS", 60),
            mock.patch.object(dashboard.time, "monotonic", return_value=100.0),
        ):
            dashboard.record_login_failure("client")
            self.assertEqual(dashboard.login_retry_after("client"), 0)
            dashboard.record_login_failure("client")
            self.assertEqual(dashboard.login_retry_after("client"), 60)
            dashboard.clear_login_failures("client")

        self.assertNotIn("client", attempts)

    def test_app_credential_and_client_key_wrappers_remain_compatible(self):
        password_hash = generate_password_hash("password")
        old_username = dashboard.app.config.get("VOD_USERNAME")
        old_password_hash = dashboard.app.config.get("VOD_PASSWORD_HASH")
        dashboard.app.config.update(
            VOD_USERNAME="admin", VOD_PASSWORD_HASH=password_hash
        )
        try:
            self.assertTrue(dashboard.username_matches("admin"))
            self.assertFalse(dashboard.username_matches("other"))
            self.assertTrue(dashboard.password_matches("password"))
            self.assertFalse(dashboard.password_matches("wrong"))
            with dashboard.app.test_request_context(
                "/login", environ_base={"REMOTE_ADDR": "192.0.2.10"}
            ):
                self.assertEqual(dashboard.login_attempt_key(), "192.0.2.10")
        finally:
            dashboard.app.config.update(
                VOD_USERNAME=old_username,
                VOD_PASSWORD_HASH=old_password_hash,
            )

    def test_app_csrf_session_adapter_preserves_creation_and_reuse(self):
        with dashboard.app.test_request_context("/"):
            with mock.patch.object(
                dashboard.dashboard_security,
                "generate_csrf_token",
                return_value="new-token",
            ) as generator:
                self.assertEqual(dashboard.get_or_create_csrf_token(), "new-token")
                self.assertEqual(dashboard.session["csrf_token"], "new-token")
                self.assertEqual(dashboard.get_or_create_csrf_token(), "new-token")
            generator.assert_called_once_with(dashboard.secrets.token_urlsafe)

    def test_app_csrf_adapter_preserves_header_then_form_precedence(self):
        with dashboard.app.test_request_context(
            "/login",
            method="POST",
            data={"csrf_token": "form-token"},
            headers={"X-CSRF-Token": "header-token"},
        ):
            dashboard.session["csrf_token"] = "header-token"
            self.assertTrue(dashboard.request_has_valid_csrf_token())
            dashboard.session["csrf_token"] = "form-token"
            self.assertFalse(dashboard.request_has_valid_csrf_token())

        with dashboard.app.test_request_context(
            "/login", method="POST", data={"csrf_token": "form-token"}
        ):
            dashboard.session["csrf_token"] = "form-token"
            self.assertTrue(dashboard.request_has_valid_csrf_token())

    def test_app_origin_and_host_adapters_use_current_config_and_request(self):
        old_origins = dashboard.app.config.get("VOD_ALLOWED_ORIGINS")
        old_hosts = dashboard.app.config.get("VOD_TRUSTED_HOSTS")
        dashboard.app.config.update(
            VOD_ALLOWED_ORIGINS=("https://allowed.example",),
            VOD_TRUSTED_HOSTS=("dashboard.example",),
        )
        try:
            with dashboard.app.test_request_context(
                "/", base_url="http://dashboard.example:8080/",
                headers={"Origin": "https://allowed.example"},
            ):
                self.assertTrue(dashboard.request_origin_is_allowed())
                self.assertTrue(dashboard.request_host_is_allowed())
        finally:
            dashboard.app.config.update(
                VOD_ALLOWED_ORIGINS=old_origins,
                VOD_TRUSTED_HOSTS=old_hosts,
            )


if __name__ == "__main__":
    unittest.main()
