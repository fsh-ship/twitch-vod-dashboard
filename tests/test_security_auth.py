import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from werkzeug.security import generate_password_hash


_AUTH_TMP = None
_AUTH_OLD_ENV = {}
if "app" not in sys.modules:
    _AUTH_TMP = tempfile.TemporaryDirectory()
    auth_base = Path(_AUTH_TMP.name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
    ):
        _AUTH_OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(auth_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(auth_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(auth_base / "data" / "settings.json")
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"

import app as dashboard  # noqa: E402


def tearDownModule():
    if _AUTH_TMP is not None:
        _AUTH_TMP.cleanup()
        for name, value in _AUTH_OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AuthenticationAndCsrfTests(unittest.TestCase):
    USERNAME = "admin"
    PASSWORD = "correct horse battery staple"
    ORIGIN = "http://localhost"

    def setUp(self):
        keys = (
            "TESTING",
            "VOD_AUTH_DISABLED",
            "VOD_USERNAME",
            "VOD_PASSWORD_HASH",
            "VOD_ALLOWED_ORIGINS",
            "VOD_TRUSTED_HOSTS",
            "SECRET_KEY",
            "SESSION_COOKIE_HTTPONLY",
            "SESSION_COOKIE_SAMESITE",
            "SESSION_COOKIE_SECURE",
            "SESSION_COOKIE_NAME",
            "PERMANENT_SESSION_LIFETIME",
        )
        self.old_config = {key: dashboard.app.config.get(key) for key in keys}
        dashboard.app.config.update(
            TESTING=True,
            VOD_AUTH_DISABLED=False,
            VOD_USERNAME=self.USERNAME,
            VOD_PASSWORD_HASH=generate_password_hash(self.PASSWORD),
            VOD_ALLOWED_ORIGINS=(),
            VOD_TRUSTED_HOSTS=(),
            SECRET_KEY="test-secret-key-that-is-longer-than-32-characters",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            SESSION_COOKIE_SECURE=False,
            SESSION_COOKIE_NAME="vod_dashboard_session",
            PERMANENT_SESSION_LIFETIME=dashboard.timedelta(hours=12),
        )
        with dashboard.login_attempt_lock:
            dashboard.login_attempts.clear()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard.app.config.update(self.old_config)
        with dashboard.login_attempt_lock:
            dashboard.login_attempts.clear()

    def login_csrf_token(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        match = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            response.data,
        )
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def login(self, username=None, password=None, origin=None):
        csrf_token = self.login_csrf_token()
        response = self.client.post(
            "/login",
            data={
                "username": self.USERNAME if username is None else username,
                "password": self.PASSWORD if password is None else password,
                "csrf_token": csrf_token,
            },
            headers={"Origin": self.ORIGIN if origin is None else origin},
        )
        return response, csrf_token

    def authenticated_csrf_token(self):
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrf_token"]

    def test_dashboard_access_without_login_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_api_access_without_login_returns_json_401(self):
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Authentication", response.get_json()["error"])

        live_response = self.client.get(
            "/api/live/status?streamer=nika_livetv"
        )
        self.assertEqual(live_response.status_code, 401)
        self.assertIn("Authentication", live_response.get_json()["error"])

    def test_successful_login_rotates_session_state_and_sets_secure_cookie_flags(self):
        response, pre_login_token = self.login()
        self.assertEqual(response.status_code, 302)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

        status = self.client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        body = status.get_json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["username"], self.USERNAME)
        self.assertNotEqual(body["csrf_token"], pre_login_token)

    def test_failed_login_returns_401_and_does_not_authenticate(self):
        response, _ = self.login(password="wrong password")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Incorrect username or password", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)

    def test_logout_clears_authenticated_session(self):
        response, _ = self.login()
        self.assertEqual(response.status_code, 302)
        csrf_token = self.authenticated_csrf_token()
        response = self.client.post(
            "/logout",
            headers={"Origin": self.ORIGIN, "X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)

    def test_authenticated_get_is_allowed(self):
        self.assertEqual(self.login()[0].status_code, 302)
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("jobs", response.get_json())

    def test_authenticated_mutation_with_valid_csrf_token_is_allowed(self):
        self.assertEqual(self.login()[0].status_code, 302)
        csrf_token = self.authenticated_csrf_token()
        response = self.client.post(
            "/api/vod/validate",
            json={"url": "https://www.twitch.tv/videos/123456"},
            headers={"Origin": self.ORIGIN, "X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_authentication_configuration_is_not_editable_through_settings_api(self):
        self.assertEqual(self.login()[0].status_code, 302)
        csrf_token = self.authenticated_csrf_token()
        original_username = dashboard.app.config["VOD_USERNAME"]
        response = self.client.post(
            "/api/settings",
            json={
                "VOD_DASHBOARD_USERNAME": "attacker",
                "VOD_DASHBOARD_PASSWORD_HASH": "plaintext",
                "VOD_DASHBOARD_SECRET_KEY": "replacement",
                "VOD_DASHBOARD_AUTH_DISABLED": True,
            },
            headers={"Origin": self.ORIGIN, "X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertNotIn("VOD_DASHBOARD_USERNAME", body)
        self.assertEqual(dashboard.app.config["VOD_USERNAME"], original_username)
        self.assertFalse(dashboard.app.config["VOD_AUTH_DISABLED"])

    def test_missing_csrf_token_is_rejected(self):
        self.assertEqual(self.login()[0].status_code, 302)
        response = self.client.post(
            "/api/vod/validate",
            json={"url": "https://www.twitch.tv/videos/123456"},
            headers={"Origin": self.ORIGIN},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.get_json()["error"])

    def test_invalid_csrf_token_is_rejected(self):
        self.assertEqual(self.login()[0].status_code, 302)
        response = self.client.post(
            "/api/vod/validate",
            json={"url": "https://www.twitch.tv/videos/123456"},
            headers={"Origin": self.ORIGIN, "X-CSRF-Token": "invalid"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.get_json()["error"])

    def test_invalid_origin_is_rejected(self):
        self.assertEqual(self.login()[0].status_code, 302)
        csrf_token = self.authenticated_csrf_token()
        response = self.client.post(
            "/api/vod/validate",
            json={"url": "https://www.twitch.tv/videos/123456"},
            headers={"Origin": "https://attacker.invalid", "X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("origin", response.get_json()["error"])

    def test_login_requires_pre_authentication_csrf_token(self):
        response = self.client.post(
            "/login",
            data={"username": self.USERNAME, "password": self.PASSWORD},
            headers={"Origin": self.ORIGIN},
        )
        self.assertEqual(response.status_code, 403)

    def test_login_rejects_invalid_origin(self):
        csrf_token = self.login_csrf_token()
        response = self.client.post(
            "/login",
            data={
                "username": self.USERNAME,
                "password": self.PASSWORD,
                "csrf_token": csrf_token,
            },
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(response.status_code, 403)

    def test_authentication_disabled_development_mode_shows_warning(self):
        dashboard.app.config["VOD_AUTH_DISABLED"] = True
        client = dashboard.app.test_client()
        status = client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()["auth_disabled"])
        dashboard_page = client.get("/")
        self.assertEqual(dashboard_page.status_code, 200)
        self.assertIn("SECURITY WARNING", dashboard_page.get_data(as_text=True))

    def test_startup_validation_rejects_missing_authentication_secrets(self):
        with self.assertRaisesRegex(RuntimeError, "Missing environment variables"):
            dashboard.security_config_from_environment({})
        disabled = dashboard.security_config_from_environment(
            {
                "VOD_DASHBOARD_AUTH_DISABLED": "1",
                "VOD_DASHBOARD_SESSION_COOKIE_SECURE": "1",
            }
        )
        self.assertTrue(disabled["VOD_AUTH_DISABLED"])
        self.assertGreaterEqual(len(disabled["SECRET_KEY"]), 32)
        self.assertTrue(disabled["SESSION_COOKIE_SECURE"])

        child_env = dict(os.environ)
        for name in (
            "VOD_DASHBOARD_AUTH_DISABLED",
            "VOD_DASHBOARD_USERNAME",
            "VOD_DASHBOARD_PASSWORD_HASH",
            "VOD_DASHBOARD_SECRET_KEY",
        ):
            child_env.pop(name, None)
        process = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=str(Path(dashboard.__file__).resolve().parent),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Authentication is enabled by default", process.stderr)

    def test_login_brute_force_throttling(self):
        with mock.patch.object(dashboard, "LOGIN_MAX_FAILURES", 2):
            self.assertEqual(self.login(password="wrong-1")[0].status_code, 401)
            self.assertEqual(self.login(password="wrong-2")[0].status_code, 401)
            response, _ = self.login(password=self.PASSWORD)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)


if __name__ == "__main__":
    unittest.main()
