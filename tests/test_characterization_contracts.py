import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
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
        "VOD_DASHBOARD_TWITCH_COOKIE_FILE",
    ):
        _OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(import_base / "data" / "settings.json")
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)
    os.environ.pop("VOD_DASHBOARD_TWITCH_COOKIE_FILE", None)

import app as dashboard  # noqa: E402
from vod_dashboard import local_vods as local_vod_helpers  # noqa: E402
from vod_dashboard import media as media_helpers  # noqa: E402
from vod_dashboard import runtime as runtime_helpers  # noqa: E402
from vod_dashboard import settings as settings_helpers  # noqa: E402
from vod_dashboard import twitch as twitch_helpers  # noqa: E402
from vod_dashboard import youtube as youtube_helpers  # noqa: E402


def tearDownModule():
    if _IMPORT_TMP is not None:
        _IMPORT_TMP.cleanup()
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


DEFAULT_SETTINGS_KEYS = {
    "archive_file",
    "batch_postprocess_mode",
    "cookie_browser",
    "cookie_file",
    "download_path",
    "enrich_vod_dates",
    "exclude_live_streams",
    "fragments",
    "include_unknown_dates",
    "manual_upload_filename_template",
    "manual_upload_prepare_enabled",
    "manual_upload_rename_video",
    "manual_upload_write_description",
    "manual_upload_write_metadata_json",
    "merge_format",
    "move_uploaded_vods",
    "only_real_vod_urls",
    "output_template",
    "playlist_end",
    "quality",
    "streamer_file",
    "streamer_profiles",
    "strict_date_filter",
    "twitch_rate_limit",
    "uploaded_vods_folder",
    "youtube_auto_upload",
    "youtube_category_id",
    "youtube_chunk_size_mb",
    "youtube_client_secret_file",
    "youtube_description",
    "youtube_description_template",
    "youtube_enabled",
    "youtube_playlist_id",
    "youtube_privacy_status",
    "youtube_tags",
    "youtube_title_template",
    "youtube_token_file",
    "youtube_upload_mode",
    "youtube_upload_history",
    "youtube_uploaded_files",
}


class IsolatedDashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.app_dir = self.base / "project"
        self.media_root = self.base / "media"
        self.runtime_dir = self.base / "data"
        self.settings_file = self.runtime_dir / "dashboard-settings.json"
        self.app_dir.mkdir(parents=True)
        self.media_root.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)

        safe_defaults = {
            **dashboard.DEFAULT_SETTINGS,
            "download_path": str(self.media_root),
            "streamer_file": str(self.runtime_dir / "streamer.txt"),
            "archive_file": str(self.runtime_dir / "archive.txt"),
            "youtube_client_secret_file": str(self.runtime_dir / "client_secret.json"),
            "youtube_token_file": str(self.runtime_dir / "youtube-token.json"),
            "uploaded_vods_folder": str(
                self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME
            ),
        }
        self.patchers = (
            mock.patch.object(dashboard, "APP_DIR", self.app_dir),
            mock.patch.object(dashboard, "MEDIA_ROOT", self.media_root),
            mock.patch.object(dashboard, "DEFAULT_DASHBOARD_DIR", self.runtime_dir),
            mock.patch.object(dashboard, "SETTINGS_FILE", self.settings_file),
            mock.patch.object(
                dashboard, "LOCAL_SETTINGS_FILE", self.app_dir / "settings.json"
            ),
            mock.patch.object(dashboard, "LOG_FILE", self.runtime_dir / "dashboard.log"),
            mock.patch.object(
                dashboard, "FIXED_STREAMER_FILE", self.runtime_dir / "streamer.txt"
            ),
            mock.patch.object(
                dashboard, "FIXED_ARCHIVE_FILE", self.runtime_dir / "archive.txt"
            ),
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
            mock.patch.dict(
                os.environ,
                {
                    "VOD_DASHBOARD_LEGACY_SETTINGS_PATH": "",
                    "VOD_DASHBOARD_TWITCH_COOKIE_FILE": "",
                },
            ),
        )
        for patcher in self.patchers:
            patcher.start()

        self.old_config = {
            "TESTING": dashboard.app.config.get("TESTING"),
            "VOD_AUTH_DISABLED": dashboard.app.config.get("VOD_AUTH_DISABLED"),
        }
        dashboard.app.config.update(TESTING=True, VOD_AUTH_DISABLED=True)
        with dashboard.job_lock:
            dashboard.jobs.clear()
            dashboard.job_counter = 0
        with dashboard.login_attempt_lock:
            dashboard.login_attempts.clear()

        self.client = dashboard.app.test_client()
        auth_status = self.client.get("/api/auth/status")
        self.assertEqual(auth_status.status_code, 200)
        self.csrf_headers = {
            "X-CSRF-Token": auth_status.get_json()["csrf_token"]
        }

    def tearDown(self):
        dashboard.app.config.update(self.old_config)
        with dashboard.job_lock:
            dashboard.jobs.clear()
            dashboard.job_counter = 0
        with dashboard.login_attempt_lock:
            dashboard.login_attempts.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def settings(self, **updates):
        settings = {**dashboard.DEFAULT_SETTINGS, **updates}
        return dashboard.normalize_settings(settings)

    def assert_typed_keys(self, payload, expected):
        self.assertEqual(set(payload), set(expected))
        for key, expected_type in expected.items():
            self.assertIsInstance(payload[key], expected_type, key)


class RouteAndApiContractTests(IsolatedDashboardTestCase):
    def test_route_paths_methods_and_endpoint_names_are_frozen(self):
        expected = {
            ("/", "GET", "index"),
            ("/login", "GET", "login"),
            ("/login", "POST", "login"),
            ("/logout", "POST", "logout"),
            ("/api/auth/status", "GET", "api_auth_status"),
            ("/api/dashboard", "GET", "api_dashboard"),
            ("/api/state", "GET", "state"),
            ("/api/settings/status", "GET", "api_settings_status"),
            ("/api/settings", "POST", "api_settings"),
            (
                "/api/streamers/repair-newlines",
                "POST",
                "api_streamers_repair_newlines",
            ),
            ("/api/streamers/status", "GET", "api_streamers_status"),
            (
                "/api/streamers/force-fixed-path",
                "POST",
                "api_streamers_force_fixed_path",
            ),
            ("/api/streamers", "POST", "api_streamers"),
            ("/api/search", "POST", "api_search"),
            ("/api/vod/validate", "POST", "api_vod_validate"),
            ("/api/download", "POST", "api_download"),
            ("/api/jobs", "GET", "api_jobs"),
            ("/api/queue/pause", "POST", "api_pause_queue"),
            ("/api/queue/resume", "POST", "api_resume_queue"),
            (
                "/api/jobs/stop-after-current",
                "POST",
                "api_stop_after_current",
            ),
            ("/api/jobs/remove-item", "POST", "api_remove_queue_item"),
            ("/api/jobs/cancel-item", "POST", "api_cancel_queue_item"),
            ("/api/jobs/retry-item", "POST", "api_retry_queue_item"),
            ("/api/jobs/resolve-error", "POST", "api_resolve_job_error"),
            ("/api/local-videos", "GET", "api_local_videos"),
            ("/api/local-video/open", "POST", "api_local_video_open"),
            (
                "/api/local-video/mark-uploaded",
                "POST",
                "api_local_video_mark_uploaded",
            ),
            (
                "/api/local-video/move-uploaded",
                "POST",
                "api_local_video_move_uploaded",
            ),
            ("/api/local-video/delete", "POST", "api_local_video_delete"),
            ("/api/youtube/upload-local", "POST", "api_youtube_upload_local"),
            (
                "/api/manual-upload/prepare-local",
                "POST",
                "api_prepare_local_for_manual_upload",
            ),
            ("/api/youtube/status", "GET", "api_youtube_status"),
            ("/api/youtube/connect", "POST", "api_youtube_connect"),
            ("/api/youtube/playlists", "GET", "api_youtube_playlists"),
            ("/api/youtube/upload-file", "POST", "api_youtube_upload_file"),
            ("/api/youtube/preview-file", "POST", "api_youtube_preview_file"),
            ("/api/open-folder", "POST", "api_open_folder"),
        }

        actual = []
        for rule in dashboard.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                actual.append((rule.rule, method, rule.endpoint))

        registrations = Counter((path, method) for path, method, _ in actual)
        duplicates = {
            registration: count
            for registration, count in registrations.items()
            if count != 1
        }
        self.assertEqual(duplicates, {})
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected))

    def test_global_error_responses_do_not_reflect_exception_details(self):
        unsafe = '<script>alert("unsafe")</script> private-token-value'
        with mock.patch.object(
            dashboard, "render_template", side_effect=RuntimeError(unsafe)
        ), mock.patch.object(dashboard, "log_line") as log:
            html_response = self.client.get("/")

        self.assertEqual(html_response.status_code, 500)
        html = html_response.get_data(as_text=True)
        self.assertIn("RuntimeError", html)
        self.assertIn("An unexpected server error occurred.", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("private-token-value", html)
        self.assertIn(unsafe, log.call_args.args[0])

        with mock.patch.object(
            dashboard, "load_settings", side_effect=ValueError(unsafe)
        ), mock.patch.object(dashboard, "log_line"):
            api_response = self.client.get("/api/state")

        self.assertEqual(api_response.status_code, 500)
        self.assertEqual(
            api_response.get_json(),
            {"error": "ValueError: Internal server error."},
        )
        self.assertNotIn(unsafe, api_response.get_data(as_text=True))

    def test_read_only_dashboard_and_diagnostics_do_not_create_runtime_data(self):
        self.runtime_dir.rmdir()
        self.media_root.rmdir()

        responses = {
            "index": self.client.get("/"),
            "dashboard": self.client.get("/api/dashboard"),
            "state": self.client.get("/api/state"),
            "settings": self.client.get("/api/settings/status"),
            "streamers": self.client.get("/api/streamers/status"),
        }

        for name, response in responses.items():
            with self.subTest(endpoint=name):
                self.assertEqual(response.status_code, 200)

        state = responses["state"].get_json()
        self.assertFalse(state["download_path_exists"])
        self.assertFalse(state["streamer_file_exists"])
        self.assertFalse(state["archive_file_exists"])
        self.assertFalse(
            responses["settings"].get_json()["settings_parent_exists"]
        )
        streamer_status = responses["streamers"].get_json()
        self.assertFalse(streamer_status["exists"])
        self.assertFalse(streamer_status["parent_exists"])
        self.assertEqual(streamer_status["streamers"], [])
        self.assertFalse(self.runtime_dir.exists())
        self.assertFalse(self.media_root.exists())
        self.assertEqual(list(self.base.rglob(".*-write-test.tmp")), [])

    def test_read_only_api_top_level_schemas_are_frozen(self):
        auth = self.client.get("/api/auth/status").get_json()
        self.assert_typed_keys(
            auth,
            {
                "authenticated": bool,
                "auth_disabled": bool,
                "username": str,
                "csrf_token": str,
            },
        )
        self.assertTrue(auth["csrf_token"])

        with mock.patch.object(
            dashboard,
            "youtube_status",
            return_value={"connected": False},
        ), mock.patch.object(
            dashboard,
            "disk_status",
            return_value={"ok": True, "path": str(self.media_root)},
        ):
            dashboard_payload = self.client.get("/api/dashboard").get_json()
        self.assert_typed_keys(
            dashboard_payload,
            {
                "jobs_total": int,
                "jobs_active": int,
                "jobs_failed": int,
                "jobs_finished": int,
                "youtube": dict,
                "disk": dict,
                "upload_mode": str,
                "upload_chunk_mb": int,
            },
        )

        state = self.client.get("/api/state").get_json()
        self.assert_typed_keys(
            state,
            {
                "settings": dict,
                "settings_file": str,
                "local_settings_file": str,
                "persistent_settings_exists": bool,
                "streamers": list,
                "archive_count": int,
                "download_path_exists": bool,
                "streamer_file_exists": bool,
                "streamer_file_resolved": str,
                "streamer_file_forced": str,
                "archive_file_exists": bool,
                "archive_file_resolved": str,
                "archive_file_forced": str,
            },
        )

        settings_status = self.client.get("/api/settings/status").get_json()
        self.assert_typed_keys(
            settings_status,
            {
                "settings_file": str,
                "settings_exists": bool,
                "settings_parent_exists": bool,
                "local_settings_file": str,
                "legacy_candidates": list,
                "download_path": str,
                "streamer_file": str,
                "archive_file": str,
                "can_write_settings_folder": bool,
            },
        )

        streamer_status = self.client.get("/api/streamers/status").get_json()
        self.assert_typed_keys(
            streamer_status,
            {
                "streamer_file": str,
                "exists": bool,
                "parent_exists": bool,
                "count": int,
                "streamers": list,
                "legacy_candidates": list,
                "raw_preview": str,
                "has_literal_newlines": bool,
                "note": str,
                "can_write": bool,
            },
        )

        jobs = self.client.get("/api/jobs").get_json()
        self.assert_typed_keys(
            jobs,
            {"jobs": list, "queue_controls": dict, "persistence": str},
        )

        local_videos = self.client.get("/api/local-videos").get_json()
        self.assert_typed_keys(
            local_videos,
            {
                "videos": list,
                "root": str,
                "uploaded_root": str,
                "include_uploaded": bool,
                "counts": dict,
            },
        )
        self.assert_typed_keys(
            local_videos["counts"],
            {"total": int, "pending": int, "uploaded": int, "size_gb": float},
        )

        youtube_status = self.client.get("/api/youtube/status").get_json()
        self.assert_typed_keys(
            youtube_status,
            {
                "google_libs_available": bool,
                "client_secret_exists": bool,
                "client_secret_path": str,
                "client_secret_candidates": list,
                "token_exists": bool,
                "token_path": str,
                "connected": bool,
                "channel_title": str,
                "error": str,
            },
        )

        with mock.patch.object(dashboard, "list_youtube_playlists", return_value=[]):
            playlists = self.client.get("/api/youtube/playlists").get_json()
        self.assertEqual(playlists, {"playlists": []})

    def test_search_api_empty_response_schema_is_frozen(self):
        with mock.patch.object(dashboard, "run_ytdlp_json_sources", return_value=[]):
            response = self.client.post(
                "/api/search",
                json={"streamers": ["example_streamer"]},
                headers=self.csrf_headers,
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assert_typed_keys(
            payload,
            {"results": list, "errors": list, "debug": list},
        )
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["errors"], [])

    def test_search_api_delegates_to_extracted_orchestration(self):
        expected = {
            "results": [],
            "errors": [{"streamer": "Example", "error": "preserved"}],
            "debug": [{"streamer": "Example", "source": "preserved"}],
        }
        with mock.patch.object(
            twitch_helpers, "search_vods", return_value=expected
        ) as moved_search:
            response = self.client.post(
                "/api/search",
                json={
                    "streamers": [" @Example ", ""],
                    "from": "2026-08-01",
                    "to": "2026-08-31",
                    "limit": "7",
                    "include_unknown_dates": False,
                    "strict_date_filter": True,
                    "exclude_live_streams": False,
                    "only_real_vod_urls": False,
                },
                headers=self.csrf_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        moved_search.assert_called_once()
        args = moved_search.call_args.args
        kwargs = moved_search.call_args.kwargs
        self.assertEqual(args[0], ["Example"])
        self.assertIsInstance(args[1], dict)
        self.assertEqual(args[2], set())
        self.assertEqual(args[3].strftime("%Y-%m-%d"), "2026-08-01")
        self.assertEqual(args[4].strftime("%Y-%m-%d"), "2026-08-31")
        self.assertEqual(args[5:], (7, False, True, False, False))
        self.assertIs(kwargs["source_runner"], dashboard.run_ytdlp_json_sources)
        self.assertIs(kwargs["detail_runner"], dashboard.run_ytdlp_vod_detail)
        self.assertIs(kwargs["log_callback"], dashboard.log_line)


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_app_retains_runtime_path_and_lock_compatibility_aliases(self):
        paths = dashboard.RUNTIME_PATHS
        expected_aliases = {
            "APP_DIR": paths.app_dir,
            "USER_HOME": paths.user_home,
            "DEFAULT_MEDIA_ROOT": paths.default_media_root,
            "MEDIA_ROOT": paths.media_root,
            "DEFAULT_DASHBOARD_DIR": paths.dashboard_dir,
            "SETTINGS_FILE": paths.settings_file,
            "LOCAL_SETTINGS_FILE": paths.local_settings_file,
            "FIXED_STREAMER_FILE": paths.streamer_file,
            "FIXED_ARCHIVE_FILE": paths.archive_file,
            "FIXED_YOUTUBE_CLIENT_SECRET_FILE": paths.youtube_client_secret_file,
            "FIXED_YOUTUBE_TOKEN_FILE": paths.youtube_token_file,
            "FIXED_UPLOADED_VODS_FOLDER": paths.uploaded_vods_folder,
        }
        for name, expected in expected_aliases.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(dashboard, name), expected)

        self.assertEqual(dashboard.LOG_MAX_BYTES, runtime_helpers.LOG_MAX_BYTES)
        self.assertTrue(hasattr(dashboard, "LOG_FILE"))
        self.assertIs(dashboard.log_file_lock, runtime_helpers.log_file_lock)

    def test_app_log_wrapper_honors_patchable_compatibility_globals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "patched" / "dashboard.log"
            with mock.patch.object(dashboard, "LOG_FILE", log_file), mock.patch.object(
                dashboard, "LOG_MAX_BYTES", 1024
            ), mock.patch.object(runtime_helpers, "log_line", wraps=runtime_helpers.log_line) as moved_log_line:
                dashboard.log_line("compatibility log")

            moved_log_line.assert_called_once_with(
                "compatibility log", log_file, 1024
            )
            self.assertIn(
                "compatibility log", log_file.read_text(encoding="utf-8")
            )


class SettingsContractTests(IsolatedDashboardTestCase):
    def test_app_retains_settings_module_compatibility_access(self):
        repository = dashboard._settings_repository()

        self.assertIs(dashboard.to_int, settings_helpers.to_int)
        self.assertIs(dashboard.to_bool, settings_helpers.to_bool)
        self.assertEqual(
            dashboard.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            settings_helpers.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        )
        self.assertEqual(
            dashboard.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
            settings_helpers.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
        )
        self.assertIs(repository.default_settings, dashboard.DEFAULT_SETTINGS)
        self.assertEqual(repository.settings_file, self.settings_file)
        self.assertEqual(repository.media_policy.media_root, self.media_root.resolve())
        self.assertEqual(repository.default_dashboard_dir, self.runtime_dir)
        self.assertEqual(
            repository.fixed_streamer_file, self.runtime_dir / "streamer.txt"
        )
        self.assertEqual(
            repository.fixed_archive_file, self.runtime_dir / "archive.txt"
        )
        self.assertEqual(
            repository.fixed_uploaded_vods_folder,
            self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME,
        )
        normalized = dashboard.normalize_settings(
            {**dashboard.DEFAULT_SETTINGS, "download_path": "active"}
        )
        self.assertEqual(
            dashboard.base_path(normalized),
            (self.media_root / "active").resolve(),
        )

    def test_default_settings_key_set_and_important_defaults_are_frozen(self):
        self.assertEqual(set(dashboard.DEFAULT_SETTINGS), DEFAULT_SETTINGS_KEYS)
        defaults = dashboard.DEFAULT_SETTINGS
        self.assertEqual(defaults["fragments"], 8)
        self.assertEqual(defaults["playlist_end"], 150)
        self.assertEqual(defaults["quality"], "source/best")
        self.assertEqual(defaults["merge_format"], "mp4")
        self.assertFalse(defaults["youtube_enabled"])
        self.assertFalse(defaults["youtube_auto_upload"])
        self.assertTrue(defaults["move_uploaded_vods"])
        self.assertEqual(defaults["youtube_privacy_status"], "private")
        self.assertEqual(defaults["streamer_profiles"], {})
        self.assertEqual(defaults["youtube_upload_mode"], "stable")
        self.assertIn("{date_de}", defaults["youtube_title_template"])
        self.assertIn("{date_de}", defaults["youtube_description_template"])
        self.assertIn("{date_de}", defaults["manual_upload_filename_template"])

    def test_app_runtime_data_wrappers_honor_patched_paths(self):
        repository = dashboard._runtime_data_repository()
        values = self.settings(download_path="active-downloads")

        self.assertIs(
            dashboard.clean_streamer_names,
            settings_helpers.clean_streamer_names,
        )
        self.assertIs(
            dashboard.read_streamers_from_path,
            settings_helpers.read_streamers_from_path,
        )
        self.assertIs(
            dashboard.write_streamers_to_path,
            settings_helpers.write_streamers_to_path,
        )
        self.assertEqual(repository.app_dir, self.app_dir)
        self.assertEqual(repository.default_dashboard_dir, self.runtime_dir)
        self.assertEqual(repository.media_policy.media_root, self.media_root.resolve())
        self.assertEqual(repository.fixed_streamer_file, self.runtime_dir / "streamer.txt")
        self.assertEqual(repository.fixed_archive_file, self.runtime_dir / "archive.txt")
        self.assertEqual(
            repository.fixed_uploaded_vods_folder,
            self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME,
        )

        dashboard.ensure_files(values)
        self.assertEqual(
            dashboard.write_streamers(
                ["@FirstStreamer", "firststreamer", "Second_2"], values
            ),
            ["FirstStreamer", "Second_2"],
        )
        self.assertEqual(
            dashboard.read_streamers(values), ["FirstStreamer", "Second_2"]
        )
        self.assertEqual(
            dashboard.streamer_file(values), self.runtime_dir / "streamer.txt"
        )
        self.assertEqual(
            dashboard.archive_file(values), self.runtime_dir / "archive.txt"
        )
        (self.runtime_dir / "archive.txt").write_text(
            "twitch 1234567890\n", encoding="utf-8"
        )
        self.assertIn("1234567890", dashboard.archive_ids(values))

        legacy = self.app_dir / "legacy" / "streamer.txt"
        legacy.parent.mkdir()
        legacy.write_text("LegacyName\n", encoding="utf-8")
        self.assertIn(legacy, dashboard.legacy_streamer_candidates(values))

    def test_setting_type_and_legacy_value_normalization_is_frozen(self):
        normalized = dashboard.normalize_settings(
            {
                **dashboard.DEFAULT_SETTINGS,
                "fragments": "12",
                "playlist_end": "25",
                "youtube_chunk_size_mb": "96",
                "include_unknown_dates": "ja",
                "exclude_live_streams": "nein",
                "youtube_enabled": "on",
                "youtube_auto_upload": "0",
                "manual_upload_prepare_enabled": 1,
                "manual_upload_rename_video": 0,
                "twitch_rate_limit": " 5m ",
                "batch_postprocess_mode": "invalid",
            }
        )
        self.assertEqual(normalized["fragments"], 12)
        self.assertEqual(normalized["playlist_end"], 25)
        self.assertEqual(normalized["youtube_chunk_size_mb"], 96)
        self.assertIs(normalized["include_unknown_dates"], True)
        self.assertIs(normalized["exclude_live_streams"], False)
        self.assertIs(normalized["youtube_enabled"], True)
        self.assertIs(normalized["youtube_auto_upload"], False)
        self.assertIs(normalized["manual_upload_prepare_enabled"], True)
        self.assertIs(normalized["manual_upload_rename_video"], False)
        self.assertEqual(normalized["twitch_rate_limit"], "5m")
        self.assertEqual(normalized["batch_postprocess_mode"], "after_each")

    def test_settings_allowlist_and_save_load_round_trip_are_frozen(self):
        saved = dashboard.save_settings(
            {
                "fragments": 17,
                "cookie_browser": "chromium",
                "youtube_title_template": "Archive {date_de}: {title}",
                "unexpected_setting": "must not be added",
                "VOD_DASHBOARD_SECRET_KEY": "must not be added",
            }
        )
        reloaded = dashboard.load_settings()
        persisted = json.loads(self.settings_file.read_text(encoding="utf-8"))

        for payload in (saved, reloaded, persisted):
            self.assertEqual(payload["fragments"], 17)
            self.assertEqual(payload["cookie_browser"], "chromium")
            self.assertEqual(
                payload["youtube_title_template"], "Archive {date_de}: {title}"
            )
            self.assertNotIn("unexpected_setting", payload)
            self.assertNotIn("VOD_DASHBOARD_SECRET_KEY", payload)
        self.assertEqual(set(persisted), DEFAULT_SETTINGS_KEYS)

    def test_explicit_legacy_settings_preserve_compatible_values_only_by_opt_in(self):
        legacy = self.base / "legacy" / "settings.json"
        legacy.parent.mkdir()
        legacy.write_text(
            json.dumps(
                {
                    "fragments": "19",
                    "include_unknown_dates": "nein",
                    "youtube_title_template": "Legacy {date_de} {title}",
                    "legacy_extension_value": "preserved",
                    "download_path": str(self.base / "outside"),
                }
            ),
            encoding="utf-8",
        )
        os.environ["VOD_DASHBOARD_LEGACY_SETTINGS_PATH"] = str(legacy)

        loaded = dashboard.load_settings()

        self.assertEqual(loaded["fragments"], 19)
        self.assertIs(loaded["include_unknown_dates"], False)
        self.assertEqual(
            loaded["youtube_title_template"], "Legacy {date_de} {title}"
        )
        self.assertEqual(loaded["legacy_extension_value"], "preserved")
        self.assertEqual(Path(loaded["download_path"]), self.media_root.resolve())
        self.assertFalse(self.settings_file.exists())

    def test_media_root_and_legacy_base_path_normalization_are_frozen(self):
        inside = self.media_root / "active"
        normalized = dashboard.normalize_settings(
            {
                **dashboard.DEFAULT_SETTINGS,
                "download_path": "",
                "base_path": str(inside),
                "uploaded_vods_folder": str(self.base / "outside-uploaded"),
            }
        )
        self.assertEqual(Path(normalized["download_path"]), inside.resolve())
        self.assertEqual(
            Path(normalized["uploaded_vods_folder"]),
            (self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME).resolve(),
        )
        self.assertEqual(Path(normalized["streamer_file"]), self.runtime_dir / "streamer.txt")
        self.assertEqual(Path(normalized["archive_file"]), self.runtime_dir / "archive.txt")


class SettingsModuleAliasTests(unittest.TestCase):
    def test_app_default_settings_and_constants_are_module_aliases(self):
        self.assertIs(dashboard.DEFAULT_SETTINGS, settings_helpers.DEFAULT_SETTINGS)
        self.assertEqual(
            dashboard.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            settings_helpers.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
        )
        self.assertEqual(
            dashboard.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
            settings_helpers.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
        )


class TwitchContractTests(IsolatedDashboardTestCase):
    def test_app_retains_twitch_helper_compatibility_aliases(self):
        helper_names = (
            "parse_date",
            "entry_date",
            "normalize_vod_url",
            "canonical_twitch_vod_url",
            "is_real_vod_url",
            "is_live_or_upcoming_entry",
            "vod_id_from_url",
            "extract_twitch_vod_id",
            "in_range",
            "normalize_single_vod_url",
            "validate_single_vod_url",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(dashboard, name), getattr(twitch_helpers, name))

    def test_twitch_url_normalization_id_extraction_and_canonicalization(self):
        vod_id = "1234567890"
        canonical = f"https://www.twitch.tv/videos/{vod_id}"
        cases = (
            ({"id": vod_id}, canonical),
            ({"url": f"/videos/{vod_id}"}, canonical),
            ({"webpage_url": f"https://twitch.tv/videos/{vod_id}?filter=all"}, canonical),
            (f"https://www.twitch.tv/videos/{vod_id}", canonical),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(dashboard.canonical_twitch_vod_url(value), expected)

        self.assertEqual(dashboard.normalize_vod_url({"url": f"videos/{vod_id}"}), canonical)
        self.assertEqual(dashboard.extract_twitch_vod_id({"display_id": vod_id}), vod_id)
        self.assertEqual(dashboard.vod_id_from_url(f"https://twitch.tv/videos/{vod_id}"), vod_id)
        self.assertEqual(dashboard.normalize_vod_url({"url": "https://twitch.tv/live_channel"}), "")

    def test_app_retains_ytdlp_integration_compatibility_wrappers(self):
        configured = self.settings(download_path="active-downloads")
        self.assertIs(
            dashboard.ytdlp_cookie_args, twitch_helpers.ytdlp_cookie_args
        )
        self.assertIs(
            dashboard.clean_twitch_rate_limit,
            twitch_helpers.clean_twitch_rate_limit,
        )
        self.assertEqual(
            dashboard.ytdlp_base_command(), [sys.executable, "-m", "yt_dlp"]
        )

        expected = (["command"], self.base / "urls.txt")
        with mock.patch.object(
            twitch_helpers, "build_download_command", return_value=expected
        ) as moved_build:
            self.assertEqual(
                dashboard.build_download_command(["vod-url"], configured), expected
            )
        args = moved_build.call_args.args
        kwargs = moved_build.call_args.kwargs
        self.assertEqual(args, (["vod-url"], configured))
        self.assertEqual(
            kwargs["download_directory"],
            (self.media_root / "active-downloads").resolve(),
        )
        self.assertEqual(
            kwargs["archive_path"], self.runtime_dir / "archive.txt"
        )
        self.assertIs(kwargs["command_factory"], dashboard.ytdlp_base_command)
        self.assertIs(kwargs["cookie_args_factory"], dashboard.ytdlp_cookie_args)

        with mock.patch.object(
            twitch_helpers, "run_ytdlp_vod_detail", return_value={"id": "1"}
        ) as moved_detail:
            self.assertEqual(dashboard.run_ytdlp_vod_detail("vod", configured), {"id": "1"})
        self.assertIs(
            moved_detail.call_args.kwargs["command_factory"],
            dashboard.ytdlp_base_command,
        )
        self.assertIs(
            moved_detail.call_args.kwargs["cookie_args_factory"],
            dashboard.ytdlp_cookie_args,
        )

        with mock.patch.object(
            twitch_helpers, "run_ytdlp_json_sources", return_value=[]
        ) as moved_sources:
            self.assertEqual(
                dashboard.run_ytdlp_json_sources("streamer", 25, configured), []
            )
        self.assertIs(
            moved_sources.call_args.kwargs["settings_loader"],
            dashboard.load_settings,
        )
        self.assertIs(
            moved_sources.call_args.kwargs["command_factory"],
            dashboard.ytdlp_base_command,
        )
        self.assertIs(
            moved_sources.call_args.kwargs["cookie_args_factory"],
            dashboard.ytdlp_cookie_args,
        )

        compatibility_payload = {
            "id": "streamer",
            "title": "streamer",
            "entries": [],
            "_debug_sources": [],
        }
        with mock.patch.object(
            twitch_helpers,
            "run_ytdlp_json_for_streamer",
            return_value=compatibility_payload,
        ) as moved_compatibility:
            self.assertEqual(
                dashboard.run_ytdlp_json_for_streamer(
                    "streamer", configured, 25
                ),
                compatibility_payload,
            )
        self.assertIs(
            moved_compatibility.call_args.kwargs["source_runner"],
            dashboard.run_ytdlp_json_sources,
        )

    def test_date_range_and_live_upcoming_rules_are_frozen(self):
        self.assertEqual(dashboard.parse_date("2026-08-10").strftime("%Y-%m-%d"), "2026-08-10")
        self.assertEqual(dashboard.parse_date("20260810").strftime("%Y-%m-%d"), "2026-08-10")
        self.assertEqual(dashboard.parse_date("10.08.2026").strftime("%Y-%m-%d"), "2026-08-10")
        self.assertIsNone(dashboard.parse_date("not-a-date"))
        start = dashboard.parse_date("2026-08-01")
        end = dashboard.parse_date("2026-08-31")
        self.assertTrue(dashboard.in_range("2026-08-10", start, end))
        self.assertFalse(dashboard.in_range("2026-09-01", start, end))
        self.assertTrue(dashboard.in_range("unknown", start, end, include_unknown=True))
        self.assertFalse(dashboard.in_range("unknown", start, end, include_unknown=False))
        self.assertTrue(dashboard.is_live_or_upcoming_entry({"live_status": "is_live"}))
        self.assertTrue(dashboard.is_live_or_upcoming_entry({"is_upcoming": True}))
        self.assertFalse(
            dashboard.is_live_or_upcoming_entry(
                {
                    "live_status": "was_live",
                    "url": "https://www.twitch.tv/videos/1234567890",
                }
            )
        )

    def test_ytdlp_download_arguments_and_cookie_precedence_are_frozen(self):
        cookie_file = self.runtime_dir / "twitch-cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        settings = self.settings(
            cookie_file=str(cookie_file),
            cookie_browser="firefox",
            fragments=6,
            quality="bestvideo+bestaudio/best",
            twitch_rate_limit="5m",
        )
        command, url_file = dashboard.build_download_command(
            ["https://www.twitch.tv/videos/1234567890"], settings
        )
        try:
            self.assertEqual(command[:3], [sys.executable, "-m", "yt_dlp"])
            self.assertEqual(command.count("--downloader"), 1)
            self.assertEqual(
                command[command.index("--downloader") + 1],
                "m3u8:ffmpeg",
            )
            self.assertIn("--cookies", command)
            self.assertEqual(command[command.index("--cookies") + 1], str(cookie_file))
            self.assertNotIn("--cookies-from-browser", command)
            self.assertEqual(command[command.index("-N") + 1], "6")
            self.assertEqual(
                command[command.index("-f") + 1], "bestvideo+bestaudio/best"
            )
            self.assertEqual(command[command.index("--limit-rate") + 1], "5M")
            self.assertEqual(Path(command[command.index("-P") + 1]), self.media_root)
            self.assertEqual(
                Path(command[command.index("--download-archive") + 1]),
                self.runtime_dir / "archive.txt",
            )
            self.assertEqual(
                url_file.read_text(encoding="utf-8"),
                "https://www.twitch.tv/videos/1234567890\n",
            )
        finally:
            url_file.unlink(missing_ok=True)

        self.assertEqual(
            dashboard.ytdlp_cookie_args(
                self.settings(cookie_file="", cookie_browser="firefox")
            ),
            ["--cookies-from-browser", "firefox"],
        )
        with self.assertRaisesRegex(RuntimeError, "Cookie file not found"):
            dashboard.ytdlp_cookie_args(
                self.settings(cookie_file=str(self.runtime_dir / "missing.txt"))
            )

    def test_source_fallback_dedup_archive_marking_and_search_schema(self):
        archived_id = "1234567890"
        second_id = "2345678901"
        (self.runtime_dir / "archive.txt").write_text(
            f"twitch {archived_id}\n", encoding="utf-8"
        )
        failed = SimpleNamespace(returncode=1, stdout="", stderr="first source failed")
        second_source = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "entries": [
                        {
                            "id": archived_id,
                            "title": "Archived VOD",
                            "upload_date": "20260810",
                            "url": f"https://www.twitch.tv/videos/{archived_id}",
                            "live_status": "was_live",
                        },
                        {
                            "id": archived_id,
                            "title": "Duplicate entry",
                            "upload_date": "20260810",
                            "url": f"https://www.twitch.tv/videos/{archived_id}",
                        },
                    ]
                }
            ),
        )
        third_source = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "entries": [
                        {
                            "id": archived_id,
                            "title": "Cross-source duplicate",
                            "upload_date": "20260810",
                            "url": f"https://www.twitch.tv/videos/{archived_id}",
                        },
                        {
                            "id": second_id,
                            "title": "Second VOD",
                            "upload_date": "20260809",
                            "url": f"https://www.twitch.tv/videos/{second_id}",
                            "live_status": "was_live",
                        },
                        {
                            "id": "3456789012",
                            "title": "Currently live",
                            "upload_date": "20260811",
                            "url": "https://www.twitch.tv/example_streamer",
                            "is_live": True,
                        },
                    ]
                }
            ),
        )

        with mock.patch.object(
            dashboard.subprocess,
            "run",
            side_effect=[failed, second_source, third_source],
        ) as run:
            response = self.client.post(
                "/api/search",
                json={
                    "streamers": ["example_streamer"],
                    "from": "2026-08-01",
                    "to": "2026-08-11",
                    "limit": 25,
                },
                headers=self.csrf_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--flat-playlist", command)
            self.assertEqual(command[command.index("--playlist-end") + 1], "25")
            self.assertEqual(call.kwargs["timeout"], 180)

        payload = response.get_json()
        self.assertEqual(set(payload), {"results", "errors", "debug"})
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["results"]), 2)
        by_id = {item["id"]: item for item in payload["results"]}
        self.assertEqual(set(by_id), {archived_id, second_id})
        self.assertTrue(by_id[archived_id]["already_downloaded"])
        self.assertFalse(by_id[second_id]["already_downloaded"])
        expected_result_keys = {
            "streamer",
            "title",
            "date",
            "url",
            "id",
            "already_downloaded",
            "date_enriched",
            "outside_range",
        }
        for item in payload["results"]:
            self.assertEqual(set(item), expected_result_keys)
            self.assertTrue(item["url"].startswith("https://www.twitch.tv/videos/"))
        self.assertEqual(len(payload["debug"]), 1)
        self.assertEqual(
            set(payload["debug"][0]),
            {
                "streamer",
                "source",
                "found_raw",
                "deduped",
                "kept",
                "unknown_dates",
                "skipped_by_date",
                "skipped_live",
                "skipped_nonvod",
            },
        )


class LocalVodContractTests(IsolatedDashboardTestCase):
    VIDEO_PAYLOAD_KEYS = {
        "path",
        "name",
        "folder",
        "relative_folder",
        "size_gb",
        "size_bytes",
        "mtime",
        "streamer",
        "date_de",
        "title",
        "vod_id",
        "youtube_title",
        "youtube_description",
        "description_file",
        "description_file_exists",
        "metadata_file",
        "metadata_file_exists",
        "marker_file",
        "prepared",
        "dashboard_uploaded",
        "manually_uploaded",
        "already_uploaded",
        "in_uploaded_folder",
        "status",
        "uploaded_at",
        "local_file_exists",
    }

    def make_video(self, name="2026-08-10 - Example - Stream [1234567890].mp4"):
        video = self.media_root / "Example" / name
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"test video")
        video.with_suffix(".info.json").write_text(
            json.dumps(
                {
                    "id": "1234567890",
                    "title": "A Test Stream",
                    "uploader": "Example",
                    "upload_date": "20260810",
                    "duration": 3661,
                    "webpage_url": "https://www.twitch.tv/videos/1234567890",
                }
            ),
            encoding="utf-8",
        )
        return video

    def test_local_vod_payload_and_uploaded_marker_contract(self):
        video = self.make_video()
        marker = dashboard.write_local_upload_marker(video, method="manual")
        payload = dashboard.local_video_metadata_payload(video, self.settings(), set())

        self.assertEqual(set(payload), self.VIDEO_PAYLOAD_KEYS)
        self.assertEqual(marker["method"], "manual")
        self.assertEqual(payload["vod_id"], "1234567890")
        self.assertEqual(payload["date_de"], "10.08.2026")
        self.assertTrue(payload["manually_uploaded"])
        self.assertTrue(payload["already_uploaded"])
        self.assertEqual(payload["status"], "Manually Uploaded")
        self.assertIsInstance(payload["size_bytes"], int)
        self.assertIsInstance(payload["size_gb"], float)

    def test_app_retains_local_vod_service_compatibility_wrappers(self):
        video = self.make_video().resolve()
        settings = self.settings()
        expected_payload = {"path": str(video), "status": "Ready"}
        with mock.patch.object(
            local_vod_helpers,
            "local_video_metadata_payload",
            return_value=expected_payload,
        ) as moved_payload:
            self.assertEqual(
                dashboard.local_video_metadata_payload(
                    video, settings, {str(video)}
                ),
                expected_payload,
            )
        self.assertEqual(
            moved_payload.call_args.args,
            (video, settings, {str(video)}),
        )
        self.assertEqual(
            moved_payload.call_args.kwargs["media_policy"].media_root,
            self.media_root.resolve(),
        )
        self.assertIs(
            moved_payload.call_args.kwargs["metadata_loader"],
            dashboard.metadata_from_path,
        )
        self.assertIs(
            moved_payload.call_args.kwargs["youtube_metadata_builder"],
            dashboard.build_youtube_metadata,
        )
        self.assertIs(
            moved_payload.call_args.kwargs["marker_reader"],
            dashboard.read_local_upload_marker,
        )
        self.assertIs(
            moved_payload.call_args.kwargs["sidecar_loader"],
            dashboard.local_video_sidecars,
        )

        expected_listing = {
            "videos": [],
            "root": str(self.media_root),
            "uploaded_root": str(
                self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME
            ),
            "include_uploaded": True,
            "counts": {
                "total": 0,
                "pending": 0,
                "uploaded": 0,
                "size_gb": 0.0,
            },
        }
        with mock.patch.object(
            local_vod_helpers,
            "enumerate_local_vods",
            return_value=expected_listing,
        ) as moved_listing:
            self.assertEqual(
                dashboard.enumerate_local_vods(settings, True),
                expected_listing,
            )
        self.assertEqual(moved_listing.call_args.args, (settings, True))
        self.assertEqual(
            moved_listing.call_args.kwargs["media_policy"].media_root,
            self.media_root.resolve(),
        )
        self.assertEqual(
            moved_listing.call_args.kwargs["uploaded_folder_fallback"],
            self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME,
        )
        self.assertEqual(
            moved_listing.call_args.kwargs["app_dir"], self.app_dir
        )
        self.assertIs(
            moved_listing.call_args.kwargs["payload_builder"],
            dashboard.local_video_metadata_payload,
        )

    def test_local_videos_route_delegates_query_and_settings(self):
        expected = {
            "videos": [{"path": "delegated.mp4"}],
            "root": "media",
            "uploaded_root": "uploaded",
            "include_uploaded": True,
            "counts": {
                "total": 1,
                "pending": 1,
                "uploaded": 0,
                "size_gb": 0.0,
            },
        }
        with mock.patch.object(
            dashboard, "enumerate_local_vods", return_value=expected
        ) as service:
            response = self.client.get(
                "/api/local-videos?include_uploaded=yes"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        service.assert_called_once()
        self.assertIsInstance(service.call_args.args[0], dict)
        self.assertIs(service.call_args.args[1], True)

    def test_sidecar_detection_is_frozen(self):
        video = self.make_video()
        video.with_suffix(".youtube.json").write_text("{}", encoding="utf-8")
        video.with_suffix(".youtube-beschreibung.txt").write_text(
            "description", encoding="utf-8"
        )
        dashboard.write_local_upload_marker(video)

        sidecars = dashboard.local_video_sidecars(video)
        self.assertEqual(
            {
                tuple(path.suffixes[-2:])
                if path.name.endswith(".json")
                else path.suffix
                for path in sidecars
            },
            {
                (".info", ".json"),
                (".youtube", ".json"),
                ".txt",
                (".uploaded", ".json"),
            },
        )
        self.assertEqual(len(sidecars), 4)

    def test_move_bundle_preserves_relative_folder_and_sidecars(self):
        video = self.make_video()
        video.with_suffix(".youtube.json").write_text("{}", encoding="utf-8")
        video.with_suffix(".youtube-beschreibung.txt").write_text(
            "description", encoding="utf-8"
        )
        dashboard.write_local_upload_marker(video)

        result = dashboard.move_video_bundle_verified(video, self.settings())
        moved = Path(result["new_path"])

        self.assertTrue(result["ok"])
        self.assertTrue(result["source_removed"])
        self.assertFalse(video.exists())
        self.assertEqual(
            moved,
            self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME / "Example" / video.name,
        )
        self.assertTrue(moved.exists())
        self.assertTrue(moved.with_suffix(".info.json").exists())
        self.assertTrue(moved.with_suffix(".youtube.json").exists())
        self.assertTrue(moved.with_suffix(".youtube-beschreibung.txt").exists())
        moved_marker = json.loads(
            moved.with_suffix(".uploaded.json").read_text(encoding="utf-8")
        )
        self.assertEqual(moved_marker["video_path"], str(moved))
        self.assertEqual(moved_marker["video_name"], moved.name)

    def test_delete_bundle_removes_video_and_known_sidecars(self):
        video = self.make_video()
        video.with_suffix(".youtube.json").write_text("{}", encoding="utf-8")
        dashboard.write_local_upload_marker(video)
        files = [video] + dashboard.local_video_sidecars(video)

        result = dashboard.delete_video_bundle_permanently(video, self.settings())

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(set(result["deleted"]), {str(path) for path in files})
        self.assertTrue(all(not path.exists() for path in files))

    def test_local_open_folder_uses_startfile(self):
        video = self.make_video()
        with mock.patch.object(dashboard.os, "startfile", create=True) as startfile:
            response = self.client.post(
                "/api/local-video/open",
                json={"path": str(video), "mode": "folder"},
                headers=self.csrf_headers,
            )
        self.assertEqual(response.status_code, 200)
        startfile.assert_called_once_with(str(video.parent.resolve()))

    def test_local_select_uses_windows_explorer(self):
        video = self.make_video()
        actual_os_name = dashboard.os.name
        with mock.patch.object(
            dashboard, "is_windows_platform", return_value=True
        ), mock.patch.object(dashboard.subprocess, "Popen") as popen:
            response = self.client.post(
                "/api/local-video/open",
                json={"path": str(video), "mode": "select"},
                headers=self.csrf_headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dashboard.os.name, actual_os_name)
        popen.assert_called_once_with(["explorer.exe", "/select,", str(video.resolve())])

    def test_local_select_uses_parent_folder_off_windows(self):
        video = self.make_video()
        with mock.patch.object(
            dashboard, "is_windows_platform", return_value=False
        ), mock.patch.object(
            dashboard.os, "startfile", create=True
        ) as startfile, mock.patch.object(dashboard.subprocess, "Popen") as popen:
            response = self.client.post(
                "/api/local-video/open",
                json={"path": str(video), "mode": "select"},
                headers=self.csrf_headers,
            )
        self.assertEqual(response.status_code, 200)
        startfile.assert_called_once_with(str(video.parent.resolve()))
        popen.assert_not_called()


class MediaCompatibilityTests(IsolatedDashboardTestCase):
    def test_app_retains_media_policy_compatibility_access(self):
        video = self.media_root / "nested" / "compatibility.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")
        settings = self.settings(
            download_path="active-downloads",
            uploaded_vods_folder="uploaded",
        )
        policy = media_helpers.MediaPathPolicy(self.media_root)

        self.assertIs(dashboard.VIDEO_EXTENSIONS, media_helpers.VIDEO_EXTENSIONS)
        self.assertEqual(
            dashboard.resolve_media_path(video, must_exist=True, require_file=True),
            policy.resolve_media_path(video, must_exist=True, require_file=True),
        )
        self.assertEqual(
            dashboard.normalize_media_directory(
                "nested", self.media_root
            ),
            policy.normalize_media_directory("nested", self.media_root),
        )
        self.assertEqual(dashboard.download_path(settings), policy.download_path(settings))
        self.assertEqual(
            dashboard.uploaded_vods_folder(settings),
            policy.uploaded_vods_folder(
                settings, self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME
            ),
        )
        self.assertEqual(
            dashboard.safe_local_video_path(video, settings),
            policy.safe_local_video_path(video, settings),
        )
        self.assertTrue(dashboard.is_path_inside(video, self.media_root))
        self.assertEqual(
            dashboard.local_video_marker_path(video),
            media_helpers.local_video_marker_path(video),
        )
        self.assertEqual(
            dashboard.local_video_sidecars(video),
            policy.local_video_sidecars(video),
        )
        self.assertEqual(
            dashboard.unique_path(video),
            media_helpers.unique_path(video),
        )


class YouTubeContractTests(IsolatedDashboardTestCase):
    def make_video(self):
        video = self.media_root / "ExampleStreamer" / "original.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"test video")
        video.with_suffix(".info.json").write_text(
            json.dumps(
                {
                    "id": "1234567890",
                    "title": "A Great Stream",
                    "uploader": "ExampleStreamer",
                    "upload_date": "20260304",
                    "duration": 3723,
                    "webpage_url": "https://www.twitch.tv/videos/1234567890",
                }
            ),
            encoding="utf-8",
        )
        return video

    def test_disconnected_status_and_playlist_contract(self):
        status = dashboard.youtube_status(self.settings())
        self.assertFalse(status["connected"])
        self.assertFalse(status["token_exists"])
        self.assertEqual(status["channel_title"], "")
        self.assertEqual(status["error"], "")

        with mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=dashboard.YouTubeNotConnectedError("YouTube is not connected."),
        ):
            response = self.client.get("/api/youtube/playlists")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"playlists": []})

    def test_app_retains_youtube_connection_compatibility_access(self):
        self.assertIs(
            dashboard.YouTubeNotConnectedError,
            youtube_helpers.YouTubeNotConnectedError,
        )
        self.assertIs(dashboard.YOUTUBE_SCOPES, youtube_helpers.YOUTUBE_SCOPES)
        self.assertIs(
            dashboard.MediaFileUpload, youtube_helpers.MediaFileUpload
        )

        settings = self.settings()
        credentials = object()
        token_path = self.base / "patched-token.json"
        secret_path = self.base / "patched-secret.json"
        with mock.patch.object(
            dashboard, "youtube_token_file", return_value=token_path
        ), mock.patch.object(
            dashboard, "youtube_client_secret_file", return_value=secret_path
        ), mock.patch.object(
            youtube_helpers,
            "get_youtube_credentials",
            return_value=credentials,
        ) as moved_credentials, mock.patch.dict(
            os.environ,
            {"VOD_DASHBOARD_YOUTUBE_OAUTH_MODE": "external"},
        ):
            self.assertIs(
                dashboard.get_youtube_credentials(settings, True),
                credentials,
            )
        self.assertEqual(
            moved_credentials.call_args.args, (settings, True)
        )
        self.assertEqual(
            moved_credentials.call_args.kwargs["token_path"], token_path
        )
        self.assertEqual(
            moved_credentials.call_args.kwargs["secret_path"], secret_path
        )
        self.assertFalse(
            moved_credentials.call_args.kwargs["interactive_oauth_allowed"]
        )
        self.assertIs(
            moved_credentials.call_args.kwargs["settings_loader"],
            dashboard.load_settings,
        )
        self.assertIs(
            moved_credentials.call_args.kwargs["settings_saver"],
            dashboard.save_settings,
        )

        service = object()
        with mock.patch.object(
            youtube_helpers, "get_youtube_service", return_value=service
        ) as moved_service:
            self.assertIs(
                dashboard.get_youtube_service(settings, True), service
            )
        self.assertEqual(moved_service.call_args.args, (settings, True))
        self.assertIs(
            moved_service.call_args.kwargs["credentials_getter"],
            dashboard.get_youtube_credentials,
        )

        playlists = [{"id": "playlist", "title": "Playlist"}]
        with mock.patch.object(
            youtube_helpers,
            "list_youtube_playlists",
            return_value=playlists,
        ) as moved_playlists:
            self.assertEqual(
                dashboard.list_youtube_playlists(settings), playlists
            )
        self.assertEqual(moved_playlists.call_args.args, (settings,))
        self.assertIs(
            moved_playlists.call_args.kwargs["service_getter"],
            dashboard.get_youtube_service,
        )

    def test_youtube_status_route_remains_a_thin_adapter(self):
        expected = {
            "google_libs_available": True,
            "client_secret_exists": True,
            "client_secret_path": "secret.json",
            "client_secret_candidates": ["secret.json"],
            "token_exists": True,
            "token_path": "token.json",
            "connected": True,
            "channel_title": "Test Channel",
            "error": "",
        }
        with mock.patch.object(
            dashboard, "youtube_status", return_value=expected
        ) as status_helper:
            response = self.client.get("/api/youtube/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        status_helper.assert_called_once()
        self.assertIsInstance(status_helper.call_args.args[0], dict)

    def test_youtube_connect_route_success_and_error_contracts(self):
        service = mock.Mock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "Test Channel"}}]
        }
        status = {"connected": True, "channel_title": "Test Channel"}
        with mock.patch.object(
            dashboard, "get_youtube_service", return_value=service
        ) as service_helper, mock.patch.object(
            dashboard, "youtube_status", return_value=status
        ):
            response = self.client.post(
                "/api/youtube/connect", headers=self.csrf_headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": True,
                "channel_title": "Test Channel",
                "status": status,
            },
        )
        self.assertTrue(service_helper.call_args.kwargs["interactive"])

        error_payload = {
            "ok": False,
            "error": "RuntimeError: OAuth failed",
            "hint": "Check the OAuth configuration.",
        }
        with mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=RuntimeError("OAuth failed"),
        ), mock.patch.object(
            dashboard,
            "youtube_connect_error_payload",
            return_value=error_payload,
        ) as error_helper, mock.patch.object(dashboard, "log_line"):
            response = self.client.post(
                "/api/youtube/connect", headers=self.csrf_headers
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), error_payload)
        self.assertIsInstance(
            error_helper.call_args.args[0], RuntimeError
        )

    def test_metadata_and_template_generation_contract(self):
        video = self.make_video()
        metadata = dashboard.build_youtube_metadata(video, self.settings())

        self.assertEqual(set(metadata), {"title", "description", "meta"})
        self.assertEqual(
            metadata["title"], "ExampleStreamer VOD - 04.03.2026 - A Great Stream"
        )
        self.assertIn("Streamer: ExampleStreamer", metadata["description"])
        self.assertIn("Date: 04.03.2026", metadata["description"])
        self.assertEqual(
            metadata["meta"],
            {
                "title": "A Great Stream",
                "streamer": "ExampleStreamer",
                "date": "2026-03-04",
                "date_de": "04.03.2026",
                "vod_id": "1234567890",
                "url": "https://www.twitch.tv/videos/1234567890",
                "duration": "01:02:03",
                "filename": "original.mp4",
                "filepath": str(video.resolve()),
            },
        )

    def test_app_retains_local_youtube_helper_compatibility_access(self):
        pure_helpers = (
            "format_duration",
            "safe_filename_title",
            "apply_youtube_template",
            "sanitize_windows_filename",
            "guess_video_title",
        )
        for name in pure_helpers:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(dashboard, name), getattr(youtube_helpers, name)
                )

        video = self.make_video()
        settings = self.settings()
        with mock.patch.object(
            youtube_helpers, "parse_info_json", return_value={"id": "1"}
        ) as moved_parse:
            self.assertEqual(dashboard.parse_info_json(video), {"id": "1"})
        self.assertEqual(moved_parse.call_args.args, (video,))
        self.assertEqual(
            moved_parse.call_args.kwargs["media_policy"].media_root,
            self.media_root.resolve(),
        )
        self.assertIs(
            moved_parse.call_args.kwargs["log_callback"], dashboard.log_line
        )

        expected_meta = {"title": "moved"}
        with mock.patch.object(
            youtube_helpers, "metadata_from_path", return_value=expected_meta
        ) as moved_metadata:
            self.assertEqual(
                dashboard.metadata_from_path(video, settings), expected_meta
            )
        self.assertIs(
            moved_metadata.call_args.kwargs["info_loader"],
            dashboard.parse_info_json,
        )
        self.assertIs(
            moved_metadata.call_args.kwargs["entry_date_parser"],
            dashboard.entry_date,
        )

        expected_youtube = {
            "title": "moved",
            "description": "",
            "meta": {},
        }
        with mock.patch.object(
            youtube_helpers,
            "build_youtube_metadata",
            return_value=expected_youtube,
        ) as moved_build:
            self.assertEqual(
                dashboard.build_youtube_metadata(video, settings),
                expected_youtube,
            )
        self.assertIs(
            moved_build.call_args.kwargs["metadata_loader"],
            dashboard.metadata_from_path,
        )
        self.assertIs(
            moved_build.call_args.kwargs["template_renderer"],
            dashboard.apply_youtube_template,
        )

        with mock.patch.object(
            youtube_helpers,
            "manual_upload_filename",
            return_value="manual-name",
        ) as moved_filename:
            self.assertEqual(
                dashboard.manual_upload_filename(
                    video, settings, expected_youtube
                ),
                "manual-name",
            )
        self.assertIs(
            moved_filename.call_args.kwargs["filename_sanitizer"],
            dashboard.sanitize_windows_filename,
        )

        with mock.patch.object(
            youtube_helpers,
            "prepare_file_for_manual_youtube_upload",
            return_value=video,
        ) as moved_prepare:
            self.assertEqual(
                dashboard.prepare_file_for_manual_youtube_upload(
                    video, settings, job_id="job-1"
                ),
                video,
            )
        self.assertEqual(
            moved_prepare.call_args.args, (video, settings, "job-1")
        )
        self.assertIs(
            moved_prepare.call_args.kwargs["metadata_builder"],
            dashboard.build_youtube_metadata,
        )
        self.assertIs(
            moved_prepare.call_args.kwargs["collision_resolver"],
            dashboard.unique_path,
        )
        self.assertIs(
            moved_prepare.call_args.kwargs["job_log_callback"],
            dashboard.append_job_log,
        )

    def test_app_retains_youtube_upload_compatibility_wrappers(self):
        settings = self.settings()
        video = self.make_video().resolve()

        with mock.patch.object(
            youtube_helpers, "youtube_chunk_mb", return_value=17
        ) as moved_chunk:
            self.assertEqual(dashboard.youtube_chunk_mb(settings), 17)
        self.assertEqual(moved_chunk.call_args.args, (settings,))
        self.assertIs(
            moved_chunk.call_args.kwargs["int_parser"], dashboard.to_int
        )

        with mock.patch.object(
            youtube_helpers, "youtube_mode_label", return_value="Mode"
        ) as moved_mode:
            self.assertEqual(dashboard.youtube_mode_label(settings), "Mode")
        self.assertIs(
            moved_mode.call_args.kwargs["chunk_size_getter"],
            dashboard.youtube_chunk_mb,
        )

        with mock.patch.object(
            youtube_helpers,
            "upload_video_to_youtube",
            return_value="youtube-id",
        ) as moved_upload:
            self.assertEqual(
                dashboard.upload_video_to_youtube(
                    video, settings, job_id="job-1"
                ),
                "youtube-id",
            )
        self.assertEqual(
            moved_upload.call_args.args, (video, settings, "job-1")
        )
        self.assertEqual(
            moved_upload.call_args.kwargs["media_policy"].media_root,
            self.media_root.resolve(),
        )
        self.assertIs(
            moved_upload.call_args.kwargs["service_getter"],
            dashboard.get_youtube_service,
        )
        self.assertIs(
            moved_upload.call_args.kwargs["media_upload_factory"],
            dashboard.MediaFileUpload,
        )
        self.assertIs(
            moved_upload.call_args.kwargs["history_recorder"],
            dashboard.remember_youtube_uploaded_file,
        )
        self.assertIs(
            moved_upload.call_args.kwargs["move_after_upload"],
            dashboard.move_uploaded_vod_to_done_folder,
        )

        with mock.patch.object(
            youtube_helpers, "remember_youtube_uploaded_file"
        ) as moved_history:
            dashboard.remember_youtube_uploaded_file(video)
        self.assertEqual(moved_history.call_args.args, (video,))
        self.assertEqual(
            moved_history.call_args.kwargs["settings_file"],
            self.settings_file,
        )
        self.assertIs(
            moved_history.call_args.kwargs["settings_loader"],
            dashboard.load_settings,
        )

        moved_path = video.with_name("moved.mp4")
        with mock.patch.object(
            youtube_helpers,
            "move_uploaded_vod_to_done_folder",
            return_value=moved_path,
        ) as moved_move:
            self.assertEqual(
                dashboard.move_uploaded_vod_to_done_folder(
                    video, settings, job_id="job-1"
                ),
                moved_path,
            )
        self.assertEqual(
            moved_move.call_args.args, (video, settings, "job-1")
        )
        self.assertIs(
            moved_move.call_args.kwargs["move_bundle"],
            dashboard.move_video_bundle_verified,
        )

    def test_manual_preparation_filename_and_sidecars_are_frozen(self):
        video = self.make_video()
        prepared = dashboard.prepare_file_for_manual_youtube_upload(
            video, self.settings()
        )

        self.assertEqual(
            prepared.name,
            "04.03.2026 - ExampleStreamer - A Great Stream.mp4",
        )
        self.assertFalse(video.exists())
        self.assertTrue(prepared.exists())
        self.assertTrue(prepared.with_suffix(".info.json").exists())
        description = prepared.with_suffix(".youtube-beschreibung.txt")
        metadata_file = prepared.with_suffix(".youtube.json")
        self.assertTrue(description.exists())
        self.assertTrue(metadata_file.exists())
        self.assertIn("YouTube Title:", description.read_text(encoding="utf-8"))
        persisted = json.loads(metadata_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["meta"]["date_de"], "04.03.2026")
        self.assertEqual(persisted["meta"]["vod_id"], "1234567890")

    def test_manual_prepare_route_contract_remains_unchanged(self):
        video = self.make_video().resolve()
        prepared = video.with_name("prepared.mp4")
        with mock.patch.object(
            dashboard,
            "prepare_file_for_manual_youtube_upload",
            return_value=prepared,
        ) as prepare:
            response = self.client.post(
                "/api/manual-upload/prepare-local",
                json={"paths": [str(video)]},
                headers=self.csrf_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "prepared": [
                    {"old": str(video), "new": str(prepared)}
                ],
                "errors": [],
            },
        )
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[0], video)
        self.assertIsInstance(prepare.call_args.args[1], dict)
        self.assertIsNone(prepare.call_args.kwargs["job_id"])

    def test_upload_body_playlist_history_and_move_order_are_frozen(self):
        video = self.make_video().resolve()
        moved = (
            self.media_root / dashboard.UPLOADED_VODS_FOLDER_NAME / video.name
        ).resolve()
        settings = self.settings(
            youtube_playlist_id="playlist-123",
            youtube_privacy_status="unlisted",
            youtube_tags="twitch, archive",
            youtube_category_id="20",
        )
        events = []
        service = mock.Mock()
        upload_request = service.videos.return_value.insert.return_value

        def finish_upload():
            events.append("upload_complete")
            return None, {"id": "youtube-video-1"}

        upload_request.next_chunk.side_effect = finish_upload
        service.playlistItems.return_value.insert.return_value.execute.side_effect = (
            lambda: events.append("playlist_inserted") or {}
        )

        def remember(path):
            events.append(f"remember:{Path(path).name}")

        def move(path, _settings, job_id=None):
            events.append("move")
            self.assertEqual(Path(path), video)
            self.assertIsNone(job_id)
            return moved

        media_upload = object()
        with mock.patch.object(dashboard, "get_youtube_service", return_value=service), mock.patch.object(
            dashboard, "MediaFileUpload", return_value=media_upload
        ) as media_file_upload, mock.patch.object(
            dashboard, "remember_youtube_uploaded_file", side_effect=remember
        ), mock.patch.object(
            dashboard, "move_uploaded_vod_to_done_folder", side_effect=move
        ):
            video_id = dashboard.upload_video_to_youtube(video, settings)

        self.assertEqual(video_id, "youtube-video-1")
        media_file_upload.assert_called_once_with(
            str(video),
            mimetype="video/mp4",
            chunksize=64 * 1024 * 1024,
            resumable=True,
        )
        insert_call = service.videos.return_value.insert.call_args
        self.assertEqual(insert_call.kwargs["part"], "snippet,status")
        self.assertIs(insert_call.kwargs["media_body"], media_upload)
        body = insert_call.kwargs["body"]
        self.assertEqual(body["snippet"]["tags"], ["twitch", "archive"])
        self.assertEqual(body["snippet"]["categoryId"], "20")
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")
        service.playlistItems.return_value.insert.assert_called_once_with(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": "playlist-123",
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": "youtube-video-1",
                    },
                }
            },
        )
        self.assertEqual(
            events,
            [
                "upload_complete",
                f"remember:{video.name}",
                "playlist_inserted",
                "move",
                f"remember:{moved.name}",
            ],
        )

    def test_uploaded_history_update_is_persisted(self):
        video = self.make_video().resolve()
        dashboard.save_settings({"youtube_uploaded_files": ["older.mp4"]})

        dashboard.remember_youtube_uploaded_file(video)

        loaded = dashboard.load_settings()
        persisted = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["youtube_uploaded_files"], ["older.mp4", str(video)])
        self.assertEqual(
            persisted["youtube_uploaded_files"], ["older.mp4", str(video)]
        )


class JobContractTests(IsolatedDashboardTestCase):
    def make_video(self):
        video = self.media_root / "job.mp4"
        video.write_bytes(b"test video")
        return video.resolve()

    def test_job_id_order_dictionary_schema_and_waiting_status_are_frozen(self):
        with mock.patch.object(dashboard.threading, "Thread") as thread_class:
            first = dashboard.create_job(
                ["https://www.twitch.tv/videos/1234567890"], "First"
            )
            second = dashboard.create_job(
                ["https://www.twitch.tv/videos/2345678901"], "Second"
            )

        self.assertEqual((first, second), ("1", "2"))
        self.assertEqual(
            set(dashboard.jobs[first]),
            {
                "id",
                "label",
                "status",
                "state",
                "created",
                "urls",
                "total_urls",
                "item_ids",
                "item_states",
                "item_statuses",
                "item_progress",
                "item_processed_seconds",
                "item_speed_multiplier",
                "item_speed_label",
                "item_eta_seconds",
                "item_updated_at",
                "item_total_duration_seconds",
                "item_resolved",
                "item_failure_kinds",
                "item_retry_job_ids",
                "stop_after_current",
                "log",
                "returncode",
            },
        )
        self.assertEqual(dashboard.jobs[first]["status"], "wartet")
        self.assertEqual(dashboard.jobs[first]["item_statuses"], ["wartet"])
        self.assertEqual(dashboard.jobs[first]["total_urls"], 1)
        self.assertRegex(dashboard.jobs[first]["created"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(thread_class.call_count, 2)
        for call in thread_class.call_args_list:
            self.assertTrue(call.kwargs["daemon"])
        response = self.client.get("/api/jobs").get_json()
        self.assertEqual([item["id"] for item in response["jobs"]], ["2", "1"])

    def test_job_log_is_capped_at_500_entries(self):
        dashboard.jobs["1"] = {"id": "1", "log": []}
        with mock.patch.object(dashboard, "log_line"):
            for index in range(505):
                dashboard.append_job_log("1", f"line-{index}")
        self.assertEqual(len(dashboard.jobs["1"]["log"]), 500)
        self.assertEqual(dashboard.jobs["1"]["log"][0], "line-5")
        self.assertEqual(dashboard.jobs["1"]["log"][-1], "line-504")

    def test_upload_job_running_success_and_failure_transitions_are_frozen(self):
        video = self.make_video()
        settings = self.settings(youtube_enabled=True)

        dashboard.jobs["success"] = {
            "id": "success",
            "status": "wartet",
            "urls": [str(video)],
            "log": [],
            "returncode": None,
        }

        def successful_upload(*_args, **_kwargs):
            self.assertEqual(dashboard.jobs["success"]["status"], "läuft")
            return "youtube-id"

        with mock.patch.object(dashboard, "load_settings", return_value=settings), mock.patch.object(
            dashboard, "get_youtube_service", return_value=mock.Mock()
        ), mock.patch.object(
            dashboard, "upload_video_to_youtube", side_effect=successful_upload
        ):
            dashboard.run_upload_job("success")
        self.assertEqual(dashboard.jobs["success"]["status"], "fertig")
        self.assertEqual(dashboard.jobs["success"]["returncode"], 0)

        dashboard.jobs["failure"] = {
            "id": "failure",
            "status": "wartet",
            "urls": [str(video)],
            "log": [],
            "returncode": None,
        }
        with mock.patch.object(dashboard, "load_settings", return_value=settings), mock.patch.object(
            dashboard, "get_youtube_service", return_value=mock.Mock()
        ), mock.patch.object(
            dashboard,
            "upload_video_to_youtube",
            side_effect=RuntimeError("mock upload failure"),
        ):
            dashboard.run_upload_job("failure")
        self.assertEqual(dashboard.jobs["failure"]["status"], "fehler")
        self.assertEqual(dashboard.jobs["failure"]["returncode"], 1)

        dashboard.jobs["connection-failure"] = {
            "id": "connection-failure",
            "status": "wartet",
            "urls": [str(video)],
            "log": [],
            "returncode": None,
        }
        with mock.patch.object(dashboard, "load_settings", return_value=settings), mock.patch.object(
            dashboard,
            "get_youtube_service",
            side_effect=RuntimeError("mock connection failure"),
        ):
            dashboard.run_upload_job("connection-failure")
        self.assertEqual(dashboard.jobs["connection-failure"]["status"], "fehler")
        self.assertEqual(dashboard.jobs["connection-failure"]["returncode"], -2)


class PlatformContractTests(IsolatedDashboardTestCase):
    def test_gunicorn_style_app_import_has_no_server_or_browser_side_effect(self):
        child_base = self.base / "gunicorn-import"
        env = dict(os.environ)
        env.update(
            {
                "VOD_DASHBOARD_MEDIA_ROOT": str(child_base / "media"),
                "VOD_DASHBOARD_DIR": str(child_base / "data"),
                "VOD_DASHBOARD_SETTINGS": str(child_base / "data" / "settings.json"),
                "VOD_DASHBOARD_AUTH_DISABLED": "1",
                "VOD_DASHBOARD_NO_BROWSER": "1",
            }
        )
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app import app; print(app.import_name); print(len(app.url_map._rules))",
            ],
            cwd=str(Path(dashboard.__file__).resolve().parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        lines = process.stdout.strip().splitlines()
        self.assertEqual(lines[0], "app")
        self.assertGreaterEqual(int(lines[1]), 30)
        self.assertFalse((child_base / "data").exists())
        self.assertFalse((child_base / "media").exists())

    def test_native_main_honors_no_browser_and_binds_loopback(self):
        native_base = self.base / "native-main"
        env = {
            "VOD_DASHBOARD_MEDIA_ROOT": str(native_base / "media"),
            "VOD_DASHBOARD_DIR": str(native_base / "data"),
            "VOD_DASHBOARD_SETTINGS": str(native_base / "data" / "settings.json"),
            "VOD_DASHBOARD_AUTH_DISABLED": "1",
            "VOD_DASHBOARD_NO_BROWSER": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "flask.Flask.run"
        ) as flask_run, mock.patch("webbrowser.open") as browser_open:
            runpy.run_path(str(Path(dashboard.__file__).resolve()), run_name="__main__")

        browser_open.assert_not_called()
        flask_run.assert_called_once_with(
            host="127.0.0.1", port=8787, debug=False
        )
        self.assertTrue((native_base / "media").exists())
        self.assertTrue((native_base / "data" / "streamer.txt").exists())
        self.assertTrue((native_base / "data" / "archive.txt").exists())


if __name__ == "__main__":
    unittest.main()
