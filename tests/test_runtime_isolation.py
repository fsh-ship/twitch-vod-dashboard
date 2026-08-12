import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.app_dir = self.base / "project" / "current"
        self.runtime_dir = self.base / "runtime"
        self.media_root = self.base / "media"
        self.app_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)
        self.media_root.mkdir(parents=True)

        self.settings_file = self.runtime_dir / "dashboard-settings.json"
        safe_defaults = {
            **dashboard.DEFAULT_SETTINGS,
            "download_path": str(self.media_root),
            "streamer_file": str(self.runtime_dir / "streamer.txt"),
            "archive_file": str(self.runtime_dir / "archive.txt"),
            "youtube_client_secret_file": str(self.runtime_dir / "client_secret.json"),
            "youtube_token_file": str(self.runtime_dir / "youtube-token.json"),
            "uploaded_vods_folder": str(self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME),
        }
        self.patchers = (
            mock.patch.object(dashboard, "APP_DIR", self.app_dir),
            mock.patch.object(dashboard, "MEDIA_ROOT", self.media_root),
            mock.patch.object(dashboard, "DEFAULT_DASHBOARD_DIR", self.runtime_dir),
            mock.patch.object(dashboard, "SETTINGS_FILE", self.settings_file),
            mock.patch.object(dashboard, "LOCAL_SETTINGS_FILE", self.app_dir / "settings.json"),
            mock.patch.object(dashboard, "LOG_FILE", self.runtime_dir / "dashboard.log"),
            mock.patch.object(dashboard, "FIXED_STREAMER_FILE", self.runtime_dir / "streamer.txt"),
            mock.patch.object(dashboard, "FIXED_ARCHIVE_FILE", self.runtime_dir / "archive.txt"),
            mock.patch.object(
                dashboard,
                "FIXED_YOUTUBE_CLIENT_SECRET_FILE",
                self.runtime_dir / "client_secret.json",
            ),
            mock.patch.object(
                dashboard,
                "FIXED_YOUTUBE_TOKEN_FILE",
                self.runtime_dir / "youtube-token.json",
            ),
            mock.patch.object(
                dashboard,
                "FIXED_UPLOADED_VODS_FOLDER",
                self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME,
            ),
            mock.patch.object(dashboard, "DEFAULT_SETTINGS", safe_defaults),
        )
        for patcher in self.patchers:
            patcher.start()

        self.old_legacy_path = os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)
        self.old_auth_disabled = dashboard.app.config.get("VOD_AUTH_DISABLED")
        dashboard.app.config["VOD_AUTH_DISABLED"] = True
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard.app.config["VOD_AUTH_DISABLED"] = self.old_auth_disabled
        if self.old_legacy_path is None:
            os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)
        else:
            os.environ["VOD_DASHBOARD_LEGACY_SETTINGS_PATH"] = self.old_legacy_path
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_missing_settings_does_not_read_ancestor_settings(self):
        ancestor_settings = self.app_dir.parent / "settings.json"
        ancestor_settings.write_text(json.dumps({"fragments": 99}), encoding="utf-8")

        settings = dashboard.load_settings()

        self.assertEqual(settings["fragments"], dashboard.DEFAULT_SETTINGS["fragments"])
        self.assertEqual(dashboard.legacy_settings_candidates(), [])

    def test_fresh_isolated_startup_uses_only_configured_roots(self):
        settings = dashboard.load_settings()

        self.assertFalse(self.settings_file.exists())
        self.assertEqual(Path(settings["download_path"]), self.media_root.resolve())
        self.assertEqual(Path(settings["streamer_file"]), self.runtime_dir / "streamer.txt")
        self.assertEqual(Path(settings["archive_file"]), self.runtime_dir / "archive.txt")
        self.assertEqual(dashboard.legacy_settings_candidates(), [])

    def test_explicit_legacy_settings_migration_is_supported_and_contained(self):
        legacy_file = self.base / "explicit-legacy" / "settings.json"
        legacy_file.parent.mkdir()
        legacy_file.write_text(
            json.dumps({"fragments": 23, "download_path": str(self.base / "outside")}),
            encoding="utf-8",
        )
        os.environ["VOD_DASHBOARD_LEGACY_SETTINGS_PATH"] = str(legacy_file)

        settings = dashboard.load_settings()

        self.assertEqual(dashboard.legacy_settings_candidates(), [legacy_file.resolve()])
        self.assertEqual(settings["fragments"], 23)
        self.assertEqual(Path(settings["download_path"]), self.media_root.resolve())
        self.assertFalse(self.settings_file.exists())

    def test_disconnected_youtube_playlists_returns_empty_list(self):
        with mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=dashboard.YouTubeNotConnectedError("YouTube is not connected."),
        ):
            response = self.client.get("/api/youtube/playlists")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"playlists": []})

    def test_connected_youtube_playlists_behavior_is_unchanged(self):
        service = mock.Mock()
        service.playlists.return_value.list.return_value.execute.return_value = {
            "items": [
                {"id": "playlist-1", "snippet": {"title": "Primary Archive"}},
                {"id": "playlist-2", "snippet": {"title": "Secondary Archive"}},
            ]
        }
        with mock.patch.object(dashboard, "get_youtube_service", return_value=service):
            response = self.client.get("/api/youtube/playlists")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "playlists": [
                    {"id": "playlist-1", "title": "Primary Archive"},
                    {"id": "playlist-2", "title": "Secondary Archive"},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
