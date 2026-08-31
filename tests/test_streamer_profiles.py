from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit


_IMPORT_TMP = None
if "app" not in sys.modules:
    _IMPORT_TMP = tempfile.TemporaryDirectory()
    import_base = Path(_IMPORT_TMP.name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(import_base / "data" / "settings.json")
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"

import app as dashboard
from vod_dashboard import streamer_profiles


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=2)


def entry(
    login: str,
    *,
    image_url: str = "https://images.example/avatar.png",
    filename: str = "",
    fetched_at: datetime = OLD,
) -> dict[str, str]:
    return {
        "login": login,
        "display_name": login.title(),
        "profile_image_url": image_url,
        "filename": filename,
        "content_type": "image/png" if filename else "",
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class StreamerProfileCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "streamer-avatars"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_cache(
        self, profiles: dict[str, dict[str, str]], images: dict[str, bytes]
    ) -> None:
        for filename, content in images.items():
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / filename).write_bytes(content)
        streamer_profiles.save_metadata(
            self.directory / streamer_profiles.INDEX_FILENAME, profiles
        )

    def test_login_normalization_and_deduplication_are_case_insensitive(self) -> None:
        self.assertEqual(streamer_profiles.canonical_login(" @Some_Name "), "some_name")
        self.assertEqual(
            streamer_profiles.normalize_logins(
                ["Some_Name", "some_name", "OTHER", "other", "../bad"]
            ),
            ["some_name", "other"],
        )

    def test_multiple_users_use_one_helix_request_with_repeated_login_params(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(
                {
                    "data": [
                        {
                            "login": "alpha",
                            "display_name": "Alpha",
                            "profile_image_url": "https://images.example/a.png",
                        },
                        {
                            "login": "beta",
                            "display_name": "Beta",
                            "profile_image_url": "https://images.example/b.png",
                        },
                    ]
                }
            )

        users = streamer_profiles.twitch_users_lookup(
            ["Alpha", "alpha", "BETA"], "client-id", "access-token", opener=opener
        )

        self.assertEqual([user["login"] for user in users], ["alpha", "beta"])
        self.assertEqual(len(requests), 1)
        query = parse_qs(urlsplit(requests[0][0].full_url).query)
        self.assertEqual(query["login"], ["alpha", "beta"])
        self.assertEqual(requests[0][0].get_header("Client-id"), "client-id")
        self.assertEqual(
            requests[0][0].get_header("Authorization"), "Bearer access-token"
        )

    def test_fresh_cache_avoids_twitch_lookup(self) -> None:
        self.write_cache(
            {"alpha": entry("alpha", filename="alpha.png", fetched_at=NOW)},
            {"alpha.png": b"cached"},
        )
        lookup = mock.Mock(side_effect=AssertionError("Helix must not be called"))
        cache = streamer_profiles.StreamerProfileCache(
            self.directory, user_lookup=lookup, clock=lambda: NOW
        )

        public = cache.public_profiles(
            ["Alpha"], client_id="client", access_token="token"
        )

        lookup.assert_not_called()
        self.assertEqual(public["alpha"]["avatar_url"], "/api/streamer-avatar/alpha")

    def test_unchanged_image_url_avoids_redownload(self) -> None:
        old = entry("alpha", filename="alpha.png")
        self.write_cache({"alpha": old}, {"alpha.png": b"old"})
        downloader = mock.Mock(side_effect=AssertionError("image must not be downloaded"))
        lookup = mock.Mock(
            return_value=[
                {
                    "login": "alpha",
                    "display_name": "Alpha New Case",
                    "profile_image_url": old["profile_image_url"],
                }
            ]
        )
        cache = streamer_profiles.StreamerProfileCache(
            self.directory,
            user_lookup=lookup,
            image_downloader=downloader,
            clock=lambda: NOW,
        )

        refreshed = cache.refresh(["alpha"], client_id="client", access_token="token")

        downloader.assert_not_called()
        self.assertEqual(refreshed["alpha"]["display_name"], "Alpha New Case")
        self.assertEqual((self.directory / "alpha.png").read_bytes(), b"old")

    def test_changed_url_and_missing_file_trigger_downloads(self) -> None:
        self.write_cache(
            {
                "alpha": entry(
                    "alpha",
                    image_url="https://images.example/old.png",
                    filename="alpha.png",
                ),
                "beta": entry(
                    "beta", image_url="https://images.example/beta.png", filename="beta.png"
                ),
            },
            {"alpha.png": b"old"},
        )
        lookup = mock.Mock(
            return_value=[
                {
                    "login": "alpha",
                    "display_name": "Alpha",
                    "profile_image_url": "https://images.example/new.png",
                },
                {
                    "login": "beta",
                    "display_name": "Beta",
                    "profile_image_url": "https://images.example/beta.png",
                },
            ]
        )
        downloader = mock.Mock(return_value=(b"new", "image/png", ".png"))
        cache = streamer_profiles.StreamerProfileCache(
            self.directory,
            user_lookup=lookup,
            image_downloader=downloader,
            clock=lambda: NOW,
        )

        cache.refresh(["alpha", "beta"], client_id="client", access_token="token")

        self.assertEqual(downloader.call_count, 2)
        self.assertEqual((self.directory / "alpha.png").read_bytes(), b"new")
        self.assertEqual((self.directory / "beta.png").read_bytes(), b"new")

    def test_metadata_roundtrip_and_missing_file_are_safe(self) -> None:
        index = self.directory / streamer_profiles.INDEX_FILENAME
        self.assertEqual(streamer_profiles.load_metadata(index), {})
        profiles = {"Alpha": entry("alpha", filename="alpha.png", fetched_at=NOW)}

        streamer_profiles.save_metadata(index, profiles)

        loaded = streamer_profiles.load_metadata(index)
        self.assertEqual(list(loaded), ["alpha"])
        self.assertEqual(loaded["alpha"]["filename"], "alpha.png")

    def test_twitch_failure_preserves_existing_avatar_and_metadata(self) -> None:
        original = entry("alpha", filename="alpha.png")
        self.write_cache({"alpha": original}, {"alpha.png": b"old"})
        cache = streamer_profiles.StreamerProfileCache(
            self.directory,
            user_lookup=mock.Mock(side_effect=RuntimeError("Twitch unavailable")),
            clock=lambda: NOW,
        )

        refreshed = cache.refresh(["alpha"], client_id="client", access_token="token")

        self.assertEqual(refreshed["alpha"], original)
        self.assertEqual((self.directory / "alpha.png").read_bytes(), b"old")

    def test_one_image_failure_does_not_abort_other_streamers(self) -> None:
        self.write_cache(
            {"alpha": entry("alpha", filename="alpha.png")},
            {"alpha.png": b"old-alpha"},
        )
        lookup = mock.Mock(
            return_value=[
                {
                    "login": "alpha",
                    "display_name": "Alpha",
                    "profile_image_url": "https://images.example/new-alpha.png",
                },
                {
                    "login": "beta",
                    "display_name": "Beta",
                    "profile_image_url": "https://images.example/beta.png",
                },
            ]
        )

        def download(url):
            if "new-alpha" in url:
                raise OSError("failed")
            return b"beta", "image/png", ".png"

        cache = streamer_profiles.StreamerProfileCache(
            self.directory,
            user_lookup=lookup,
            image_downloader=download,
            clock=lambda: NOW,
        )

        refreshed = cache.refresh(
            ["alpha", "beta"], client_id="client", access_token="token"
        )

        self.assertEqual((self.directory / "alpha.png").read_bytes(), b"old-alpha")
        self.assertEqual((self.directory / "beta.png").read_bytes(), b"beta")
        self.assertIn("beta", refreshed)

    def test_missing_twitch_user_is_safe_and_existing_avatar_is_retained(self) -> None:
        self.write_cache(
            {"alpha": entry("alpha", filename="alpha.png")},
            {"alpha.png": b"old"},
        )
        cache = streamer_profiles.StreamerProfileCache(
            self.directory,
            user_lookup=mock.Mock(return_value=[]),
            clock=lambda: NOW,
        )

        refreshed = cache.refresh(
            ["alpha", "missing"], client_id="client", access_token="token"
        )

        self.assertEqual((self.directory / "alpha.png").read_bytes(), b"old")
        self.assertEqual(refreshed["missing"]["filename"], "")
        self.assertTrue(streamer_profiles.metadata_is_fresh(refreshed["missing"], NOW))

    def test_unavailable_credentials_use_existing_cache_without_lookup(self) -> None:
        self.write_cache(
            {"alpha": entry("alpha", filename="alpha.png")},
            {"alpha.png": b"old"},
        )
        lookup = mock.Mock(side_effect=AssertionError("must not be called"))
        cache = streamer_profiles.StreamerProfileCache(
            self.directory, user_lookup=lookup, clock=lambda: NOW
        )

        public = cache.public_profiles(["alpha"])

        lookup.assert_not_called()
        self.assertIn("alpha", public)


class StreamerProfileRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "streamer-avatars"
        self.cache = streamer_profiles.StreamerProfileCache(
            self.directory, clock=lambda: NOW
        )
        self.old_auth_disabled = dashboard.app.config.get("VOD_AUTH_DISABLED")
        dashboard.app.config.update(TESTING=True, VOD_AUTH_DISABLED=True)
        self.client = dashboard.app.test_client()

    def tearDown(self) -> None:
        dashboard.app.config["VOD_AUTH_DISABLED"] = self.old_auth_disabled
        self.temporary.cleanup()

    def seed_avatar(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "alpha.png").write_bytes(b"avatar-bytes")
        streamer_profiles.save_metadata(
            self.cache.index_path,
            {"alpha": entry("alpha", filename="alpha.png", fetched_at=NOW)},
        )

    def test_cached_avatar_endpoint_serves_image_and_missing_returns_404(self) -> None:
        self.seed_avatar()
        with mock.patch.object(dashboard, "STREAMER_PROFILE_CACHE", self.cache):
            response = self.client.get("/api/streamer-avatar/Alpha")
            missing = self.client.get("/api/streamer-avatar/missing")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"avatar-bytes")
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(missing.status_code, 404)
        response.close()

    def test_avatar_route_cannot_traverse_or_serve_unindexed_files(self) -> None:
        self.seed_avatar()
        secret = self.directory.parent / "secret.png"
        secret.write_bytes(b"secret")
        with mock.patch.object(dashboard, "STREAMER_PROFILE_CACHE", self.cache):
            traversal = self.client.get("/api/streamer-avatar/..%2Fsecret")
            unindexed = self.client.get("/api/streamer-avatar/secret")

        self.assertEqual(traversal.status_code, 404)
        self.assertEqual(unindexed.status_code, 404)
        self.assertIsNone(self.cache.resolve_avatar("../secret"))

    def test_public_api_uses_configured_streamers_and_never_exposes_secrets(self) -> None:
        self.seed_avatar()
        with (
            mock.patch.object(dashboard, "STREAMER_PROFILE_CACHE", self.cache),
            mock.patch.object(dashboard, "load_settings", return_value={}),
            mock.patch.object(dashboard, "read_streamers", return_value=["Alpha", "alpha"]),
            mock.patch.dict(
                dashboard.os.environ,
                {
                    "TWITCH_CLIENT_ID": "super-secret-client",
                    "TWITCH_ACCESS_TOKEN": "super-secret-token",
                },
            ),
        ):
            response = self.client.get("/api/streamer-profiles")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(list(payload["profiles"]), ["alpha"])
        self.assertEqual(
            payload["profiles"]["alpha"]["avatar_url"],
            "/api/streamer-avatar/alpha",
        )
        serialized = response.get_data(as_text=True)
        self.assertNotIn("super-secret-client", serialized)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("profile_image_url", serialized)
        self.assertNotIn(str(self.directory), serialized)


if __name__ == "__main__":
    unittest.main()
