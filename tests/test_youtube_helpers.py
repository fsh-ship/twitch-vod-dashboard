import ast
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vod_dashboard import twitch
from vod_dashboard import youtube
from vod_dashboard.media import MediaPathPolicy


class LocalYouTubeHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.outside_root = self.base / "outside"
        self.media_root.mkdir()
        self.outside_root.mkdir()
        self.policy = MediaPathPolicy(self.media_root)
        self.settings = {
            "download_path": str(self.media_root),
            "youtube_title_template": (
                "{streamer} VOD - {date_de} - {title}"
            ),
            "youtube_description": "Fallback description",
            "youtube_description_template": (
                "Streamer: {streamer}\n"
                "Date: {date_de}\n"
                "Original: {url}\n"
                "VOD ID: {vod_id}\n"
                "Duration: {duration}"
            ),
            "manual_upload_prepare_enabled": True,
            "manual_upload_rename_video": True,
            "manual_upload_filename_template": (
                "{date_de} - {streamer} - {title}"
            ),
            "manual_upload_write_description": True,
            "manual_upload_write_metadata_json": True,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_video(self, name="original.mp4", folder="ExampleStreamer"):
        video = self.media_root / folder / name
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"test video")
        return video.resolve()

    @staticmethod
    def info_payload(**updates):
        payload = {
            "id": "1234567890",
            "title": "A Great Stream",
            "uploader": "ExampleStreamer",
            "upload_date": "20260304",
            "duration": 3723,
            "webpage_url": (
                "https://www.twitch.tv/videos/1234567890"
            ),
        }
        payload.update(updates)
        return payload

    def write_info(self, video, payload=None, encoding="utf-8"):
        path = video.with_suffix(".info.json")
        path.write_text(
            json.dumps(payload or self.info_payload(), ensure_ascii=False),
            encoding=encoding,
        )
        return path

    def metadata(self, video, settings=None, **kwargs):
        return youtube.metadata_from_path(
            video,
            settings or self.settings,
            media_policy=self.policy,
            entry_date_parser=twitch.entry_date,
            date_parser=twitch.parse_date,
            **kwargs,
        )

    def build_metadata(self, video, settings=None, **kwargs):
        return youtube.build_youtube_metadata(
            video,
            settings or self.settings,
            media_policy=self.policy,
            entry_date_parser=twitch.entry_date,
            date_parser=twitch.parse_date,
            **kwargs,
        )

    def prepare(self, video, settings=None, job_id=None, **kwargs):
        configured = settings or self.settings
        return youtube.prepare_file_for_manual_youtube_upload(
            video,
            configured,
            job_id,
            media_policy=self.policy,
            metadata_builder=lambda path, values: self.build_metadata(
                path, values
            ),
            **kwargs,
        )

    def test_module_boundary_has_no_framework_or_unrelated_domain_imports(self):
        module_path = Path(youtube.__file__).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        self.assertEqual(
            imported_roots,
            {
                "__future__",
                "datetime",
                "google",
                "google_auth_oauthlib",
                "googleapiclient",
                "json",
                "mimetypes",
                "os",
                "pathlib",
                "re",
                "typing",
                "unicodedata",
                "vod_dashboard",
            },
        )
        source = module_path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "import app",
            "flask",
            "subprocess",
            "urllib",
            "vod_dashboard.twitch",
            "vod_dashboard.settings",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_duration_formatting_contract(self):
        self.assertEqual(youtube.format_duration(0), "00:00")
        self.assertEqual(youtube.format_duration(65), "01:05")
        self.assertEqual(youtube.format_duration(3723), "01:02:03")
        self.assertEqual(youtube.format_duration("61.9"), "01:01")
        self.assertEqual(youtube.format_duration(None), "")
        self.assertEqual(youtube.format_duration("invalid"), "")

    def test_title_cleanup_and_guess_length_contract(self):
        path = Path("  A   spaced   title  .mp4")
        self.assertEqual(youtube.safe_filename_title(path), "A spaced title")
        long_path = Path(("x" * 120) + ".mp4")
        self.assertEqual(youtube.guess_video_title(long_path), "x" * 95)

    def test_youtube_title_sanitization_contract(self):
        cases = (
            ("A < B", "A B"),
            ("A > B", "A B"),
            ("<test>", "test"),
            ("A <<<<>>>> B", "A B"),
            ("  A   <  B\n\t C  ", "A B C"),
            ("<<<<>>>>", "YouTube Upload"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    youtube.sanitize_youtube_title(value), expected
                )

    def test_windows_filename_sanitization_contract(self):
        self.assertEqual(youtube.sanitize_windows_filename("CON"), "_CON")
        self.assertEqual(
            youtube.sanitize_windows_filename(" A😀: B — C?? "),
            "A B - C",
        )
        self.assertEqual(
            youtube.sanitize_windows_filename('<>:"/\\|?*'),
            "YouTube Upload",
        )
        self.assertEqual(
            youtube.sanitize_windows_filename("abcdefghijk", max_len=8),
            "abcdefgh",
        )

    def test_info_json_parsing_supports_utf8_bom(self):
        video = self.make_video()
        info = self.write_info(video, encoding="utf-8-sig")
        loaded = youtube.parse_info_json(
            video, media_policy=self.policy
        )
        self.assertEqual(loaded, self.info_payload())
        self.assertEqual(info.suffixes[-2:], [".info", ".json"])

    def test_malformed_info_json_logs_and_falls_back_to_empty(self):
        video = self.make_video()
        video.with_suffix(".info.json").write_text(
            "{not-json", encoding="utf-8"
        )
        logger = mock.Mock()
        loaded = youtube.parse_info_json(
            video,
            media_policy=self.policy,
            log_callback=logger,
        )
        self.assertEqual(loaded, {})
        self.assertGreaterEqual(logger.call_count, 1)
        self.assertIn("Could not read info JSON", logger.call_args.args[0])

    def test_metadata_fallback_without_info_json(self):
        video = self.make_video(
            "2026-03-04 Fallback title [1234567890].mp4"
        )
        metadata = self.metadata(video)
        self.assertEqual(metadata["streamer"], "ExampleStreamer")
        self.assertEqual(metadata["date"], "2026-03-04")
        self.assertEqual(metadata["date_de"], "04.03.2026")
        self.assertEqual(metadata["vod_id"], "1234567890")
        self.assertEqual(
            metadata["url"],
            "https://www.twitch.tv/videos/1234567890",
        )
        self.assertEqual(metadata["duration"], "")
        self.assertEqual(metadata["filepath"], str(video))

    def test_metadata_and_templates_preserve_date_de_compatibility(self):
        video = self.make_video()
        self.write_info(video)
        metadata = self.build_metadata(video)
        self.assertEqual(
            metadata["title"],
            "ExampleStreamer VOD - 04.03.2026 - A Great Stream",
        )
        self.assertEqual(metadata["meta"]["date"], "2026-03-04")
        self.assertEqual(metadata["meta"]["date_de"], "04.03.2026")
        self.assertEqual(metadata["meta"]["duration"], "01:02:03")
        self.assertIn("Date: 04.03.2026", metadata["description"])
        self.assertIn("Duration: 01:02:03", metadata["description"])

    def test_sanitized_title_matches_preview_preparation_and_upload(self):
        video = self.make_video()
        original_title = "A < B"
        self.write_info(video, self.info_payload(title=original_title))
        settings = {
            **self.settings,
            "youtube_title_template": "YouTube archive: {title} >>>",
            "manual_upload_rename_video": False,
            "move_uploaded_vods": False,
        }

        preview = self.build_metadata(video, settings)
        self.assertEqual(preview["title"], "YouTube archive: A B")
        self.assertEqual(preview["meta"]["title"], original_title)

        prepared = self.prepare(video, settings)
        persisted = json.loads(
            prepared.with_suffix(".youtube.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["title"], preview["title"])

        service = mock.Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )
        youtube.upload_video_to_youtube(
            video,
            settings,
            media_policy=self.policy,
            service_getter=mock.Mock(return_value=service),
            metadata_builder=lambda path, values: self.build_metadata(
                path, values
            ),
            media_upload_factory=mock.Mock(return_value=object()),
            history_recorder=mock.Mock(),
            move_after_upload=mock.Mock(return_value=video),
        )
        uploaded_title = service.videos.return_value.insert.call_args.kwargs[
            "body"
        ]["snippet"]["title"]
        self.assertEqual(uploaded_title, preview["title"])
        self.assertEqual(
            self.metadata(video)["title"],
            original_title,
        )

    def test_template_rendering_whitespace_and_failure_fallback(self):
        meta = {"title": "VOD", "date_de": "04.03.2026"}
        self.assertEqual(
            youtube.apply_youtube_template(
                "  {title}   X\n\n\n{date_de}  ", meta
            ),
            "VOD X\n\n04.03.2026",
        )
        self.assertEqual(
            youtube.apply_youtube_template(
                "{missing}", meta, fallback="Fallback"
            ),
            "Fallback",
        )
        self.assertEqual(
            youtube.apply_youtube_template("", meta, fallback="  raw  "),
            "  raw  ",
        )

    def test_manual_filename_generation_uses_compatibility_template(self):
        video = self.make_video()
        metadata = {
            "title": "YouTube title",
            "meta": {
                "date_de": "04.03.2026",
                "streamer": "ExampleStreamer",
                "title": "A: Great / Stream 😀",
            },
        }
        self.assertEqual(
            youtube.manual_upload_filename(video, self.settings, metadata),
            "04.03.2026 - ExampleStreamer - A Great Stream",
        )

    def test_preparation_without_rename_writes_both_sidecars(self):
        video = self.make_video()
        self.write_info(video)
        settings = {**self.settings, "manual_upload_rename_video": False}
        prepared = self.prepare(video, settings)

        self.assertEqual(prepared, video)
        self.assertTrue(video.exists())
        description_path = video.with_suffix(
            ".youtube-beschreibung.txt"
        )
        metadata_path = video.with_suffix(".youtube.json")
        self.assertTrue(description_path.exists())
        self.assertTrue(metadata_path.exists())
        self.assertEqual(
            description_path.read_text(encoding="utf-8"),
            "YouTube Title:\n"
            "ExampleStreamer VOD - 04.03.2026 - A Great Stream\n\n"
            "YouTube Description:\n"
            "Streamer: ExampleStreamer\n"
            "Date: 04.03.2026\n"
            "Original: https://www.twitch.tv/videos/1234567890\n"
            "VOD ID: 1234567890\n"
            "Duration: 01:02:03\n",
        )
        persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["meta"]["vod_id"], "1234567890")

    def test_preparation_with_rename_copies_info_and_logs_write_order(self):
        video = self.make_video()
        old_info = self.write_info(video)
        log = mock.Mock()
        prepared = self.prepare(
            video,
            job_id="job-1",
            job_log_callback=log,
        )

        self.assertEqual(
            prepared.name,
            "04.03.2026 - ExampleStreamer - A Great Stream.mp4",
        )
        self.assertFalse(video.exists())
        self.assertTrue(prepared.exists())
        self.assertTrue(old_info.exists())
        self.assertEqual(
            json.loads(
                prepared.with_suffix(".info.json").read_text(
                    encoding="utf-8"
                )
            ),
            self.info_payload(),
        )
        self.assertEqual(
            [call.args[1] for call in log.call_args_list],
            [
                "Prepare for YouTube: renamed VOD to " + prepared.name,
                "Prepare for YouTube: description saved to "
                + prepared.with_suffix(
                    ".youtube-beschreibung.txt"
                ).name,
                "Prepare for YouTube: metadata saved to "
                + prepared.with_suffix(".youtube.json").name,
            ],
        )

    def test_existing_sidecars_are_overwritten(self):
        video = self.make_video()
        self.write_info(video)
        description_path = video.with_suffix(
            ".youtube-beschreibung.txt"
        )
        metadata_path = video.with_suffix(".youtube.json")
        description_path.write_text("old description", encoding="utf-8")
        metadata_path.write_text("old metadata", encoding="utf-8")
        settings = {**self.settings, "manual_upload_rename_video": False}

        self.prepare(video, settings)

        self.assertNotEqual(
            description_path.read_text(encoding="utf-8"),
            "old description",
        )
        self.assertEqual(
            json.loads(metadata_path.read_text(encoding="utf-8"))["title"],
            "ExampleStreamer VOD - 04.03.2026 - A Great Stream",
        )

    def test_filename_collision_uses_existing_numbering_contract(self):
        video = self.make_video()
        self.write_info(video)
        desired = video.with_name(
            "04.03.2026 - ExampleStreamer - A Great Stream.mp4"
        )
        desired.write_bytes(b"existing")

        prepared = self.prepare(video)

        self.assertEqual(
            prepared.name,
            "04.03.2026 - ExampleStreamer - A Great Stream (2).mp4",
        )
        self.assertEqual(desired.read_bytes(), b"existing")

    def test_disabled_preparation_returns_validated_file_unchanged(self):
        video = self.make_video()
        settings = {
            **self.settings,
            "manual_upload_prepare_enabled": False,
        }
        metadata_builder = mock.Mock()
        prepared = youtube.prepare_file_for_manual_youtube_upload(
            video,
            settings,
            media_policy=self.policy,
            metadata_builder=metadata_builder,
        )
        self.assertEqual(prepared, video)
        metadata_builder.assert_not_called()
        self.assertFalse(video.with_suffix(".youtube.json").exists())

    def test_invalid_and_outside_root_files_are_rejected_before_writes(self):
        invalid = self.media_root / "notes.txt"
        invalid.write_text("not video", encoding="utf-8")
        outside = self.outside_root / "outside.mp4"
        outside.write_bytes(b"outside")

        for path in (invalid, outside):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    self.prepare(path)
                self.assertFalse(
                    path.with_suffix(".youtube.json").exists()
                )
                self.assertFalse(
                    path.with_suffix(
                        ".youtube-beschreibung.txt"
                    ).exists()
                )


class YouTubeConnectionHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.app_dir = self.base / "app"
        self.dashboard_dir = self.base / "data"
        self.fixed_secret = self.dashboard_dir / "client_secret.json"
        self.fixed_token = self.dashboard_dir / "youtube-token.json"
        self.app_dir.mkdir()
        self.settings = {
            "youtube_client_secret_file": str(self.fixed_secret),
            "youtube_token_file": str(self.fixed_token),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def credential_kwargs(self, **updates):
        values = {
            "token_path": self.fixed_token,
            "secret_path": self.fixed_secret,
            "libraries_available": True,
        }
        values.update(updates)
        return values

    def test_optional_dependency_unavailable_behavior(self):
        self.assertFalse(youtube.youtube_available(False))
        with self.assertRaisesRegex(
            RuntimeError, "Required Google libraries are unavailable"
        ):
            youtube.get_youtube_credentials(
                self.settings,
                **self.credential_kwargs(libraries_available=False),
            )

    def test_client_secret_candidates_and_fixed_token_path_contract(self):
        configured = self.base / "configured-secret.json"
        settings = {
            "youtube_client_secret_file": str(configured),
            "youtube_token_file": str(self.base / "ignored-token.json"),
        }
        candidates = youtube.youtube_client_secret_candidates(
            settings,
            fixed_client_secret_file=self.fixed_secret,
            default_dashboard_dir=self.dashboard_dir,
            app_dir=self.app_dir,
        )
        self.assertEqual(
            candidates,
            [
                configured,
                self.fixed_secret,
            ],
        )
        self.assertEqual(
            youtube.resolve_youtube_token_file(
                settings, fixed_token_file=self.fixed_token
            ),
            self.fixed_token,
        )
        self.assertTrue(youtube.youtube_path_is_stale("/mnt/data/old.json"))
        self.assertFalse(youtube.youtube_path_is_stale(str(configured)))

    def test_client_secret_discovery_does_not_search_application_parents(self):
        unrelated = self.app_dir.parent / "client_secret.json"
        unrelated.write_text('{"client_secret":"unrelated-secret"}', encoding="utf-8")

        candidates = youtube.youtube_client_secret_candidates(
            {},
            fixed_client_secret_file=self.fixed_secret,
            default_dashboard_dir=self.dashboard_dir,
            app_dir=self.app_dir,
        )
        resolved = youtube.resolve_youtube_client_secret_file(
            {},
            fixed_client_secret_file=self.fixed_secret,
            default_dashboard_dir=self.dashboard_dir,
            app_dir=self.app_dir,
        )

        self.assertNotIn(unrelated, candidates)
        self.assertEqual(resolved, self.fixed_secret)

    def test_client_secret_resolution_preserves_candidate_fallback_order(self):
        configured = self.base / "configured-secret.json"
        configured.write_text("{}", encoding="utf-8")
        settings = {"youtube_client_secret_file": str(configured)}
        self.assertEqual(
            youtube.resolve_youtube_client_secret_file(
                settings,
                fixed_client_secret_file=self.fixed_secret,
                default_dashboard_dir=self.dashboard_dir,
                app_dir=self.app_dir,
            ),
            configured,
        )

        configured.unlink()
        self.fixed_secret.parent.mkdir(parents=True)
        self.fixed_secret.write_text("{}", encoding="utf-8")
        self.assertEqual(
            youtube.resolve_youtube_client_secret_file(
                settings,
                fixed_client_secret_file=self.fixed_secret,
                default_dashboard_dir=self.dashboard_dir,
                app_dir=self.app_dir,
            ),
            self.fixed_secret,
        )

    def test_missing_token_and_missing_interactive_secret_errors(self):
        with self.assertRaisesRegex(
            youtube.YouTubeNotConnectedError,
            "YouTube is not connected",
        ):
            youtube.get_youtube_credentials(
                self.settings,
                **self.credential_kwargs(
                    credentials_class=mock.Mock(),
                ),
            )

        with self.assertRaisesRegex(
            RuntimeError, "client_secret.json not found"
        ):
            youtube.get_youtube_credentials(
                self.settings,
                interactive=True,
                **self.credential_kwargs(
                    credentials_class=mock.Mock(),
                    flow_class=mock.Mock(),
                ),
            )

    def test_valid_stored_credentials_are_returned(self):
        self.fixed_token.parent.mkdir(parents=True)
        self.fixed_token.write_text("{}", encoding="utf-8")
        credentials = SimpleNamespace(
            valid=True, expired=False, refresh_token=None
        )
        credentials_class = mock.Mock()
        credentials_class.from_authorized_user_file.return_value = credentials

        result = youtube.get_youtube_credentials(
            self.settings,
            **self.credential_kwargs(
                credentials_class=credentials_class,
            ),
        )

        self.assertIs(result, credentials)
        credentials_class.from_authorized_user_file.assert_called_once_with(
            str(self.fixed_token), youtube.YOUTUBE_SCOPES
        )

    def test_expired_credentials_refresh_and_persist_token(self):
        self.fixed_token.parent.mkdir(parents=True)
        self.fixed_token.write_text("old", encoding="utf-8")

        class RefreshableCredentials:
            valid = True
            expired = True
            refresh_token = "refresh-token"

            def __init__(self):
                self.requests = []

            def refresh(self, request):
                self.requests.append(request)

            def to_json(self):
                return '{"token":"refreshed"}'

        credentials = RefreshableCredentials()
        credentials_class = mock.Mock()
        credentials_class.from_authorized_user_file.return_value = credentials
        request = object()
        request_factory = mock.Mock(return_value=request)

        result = youtube.get_youtube_credentials(
            self.settings,
            **self.credential_kwargs(
                credentials_class=credentials_class,
                request_factory=request_factory,
            ),
        )

        self.assertIs(result, credentials)
        self.assertEqual(credentials.requests, [request])
        self.assertEqual(
            self.fixed_token.read_text(encoding="utf-8"),
            '{"token":"refreshed"}',
        )
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(self.fixed_token.stat().st_mode), 0o600
            )

    def test_refresh_failure_logs_and_disconnects(self):
        self.fixed_token.parent.mkdir(parents=True)
        self.fixed_token.write_text("old", encoding="utf-8")
        credentials = mock.Mock(
            valid=True, expired=True, refresh_token="refresh-token"
        )
        credentials.refresh.side_effect = RuntimeError("refresh failed")
        credentials_class = mock.Mock()
        credentials_class.from_authorized_user_file.return_value = credentials
        logger = mock.Mock()

        with self.assertRaises(youtube.YouTubeNotConnectedError):
            youtube.get_youtube_credentials(
                self.settings,
                **self.credential_kwargs(
                    credentials_class=credentials_class,
                    request_factory=mock.Mock,
                    log_callback=logger,
                ),
            )

        logger.assert_called_once_with(
            "YouTube token refresh failed; reconnect YouTube: refresh failed"
        )

    def test_interactive_oauth_persists_token_and_paths(self):
        self.fixed_secret.parent.mkdir(parents=True)
        self.fixed_secret.write_text("{}", encoding="utf-8")
        credentials = SimpleNamespace(
            valid=True,
            expired=False,
            refresh_token="refresh-token",
            to_json=lambda: '{"token":"new"}',
        )
        flow = mock.Mock()
        flow.run_local_server.return_value = credentials
        flow_class = mock.Mock()
        flow_class.from_client_secrets_file.return_value = flow
        saved = {"existing": True}
        settings_loader = mock.Mock(return_value=saved)
        settings_saver = mock.Mock()

        result = youtube.get_youtube_credentials(
            self.settings,
            interactive=True,
            **self.credential_kwargs(
                credentials_class=mock.Mock(),
                flow_class=flow_class,
                settings_loader=settings_loader,
                settings_saver=settings_saver,
            ),
        )

        self.assertIs(result, credentials)
        flow_class.from_client_secrets_file.assert_called_once_with(
            str(self.fixed_secret), youtube.YOUTUBE_SCOPES
        )
        flow.run_local_server.assert_called_once_with(
            port=0, prompt="consent"
        )
        self.assertEqual(
            self.fixed_token.read_text(encoding="utf-8"),
            '{"token":"new"}',
        )
        settings_saver.assert_called_once_with(
            {
                "existing": True,
                "youtube_client_secret_file": str(self.fixed_secret),
                "youtube_token_file": str(self.fixed_token),
            }
        )

    def test_external_mode_rejects_interactive_oauth_without_starting_listener(self):
        self.fixed_secret.parent.mkdir(parents=True)
        self.fixed_secret.write_text("{}", encoding="utf-8")
        flow_class = mock.Mock()

        with self.assertRaisesRegex(
            youtube.YouTubeOAuthBootstrapRequiredError,
            "external OAuth bootstrap",
        ):
            youtube.get_youtube_credentials(
                self.settings,
                interactive=True,
                **self.credential_kwargs(
                    credentials_class=mock.Mock(),
                    flow_class=flow_class,
                    interactive_oauth_allowed=False,
                ),
            )

        flow_class.from_client_secrets_file.assert_not_called()

    def test_oauth_mode_defaults_native_and_external_is_explicit(self):
        self.assertEqual(youtube.youtube_oauth_mode({}), "native")
        self.assertTrue(youtube.youtube_interactive_oauth_enabled({}))
        self.assertEqual(
            youtube.youtube_oauth_mode(
                {"VOD_DASHBOARD_YOUTUBE_OAUTH_MODE": " EXTERNAL "}
            ),
            "external",
        )
        self.assertFalse(
            youtube.youtube_interactive_oauth_enabled(
                {"VOD_DASHBOARD_YOUTUBE_OAUTH_MODE": "external"}
            )
        )
        with self.assertRaisesRegex(RuntimeError, "must be 'native' or 'external'"):
            youtube.youtube_oauth_mode(
                {"VOD_DASHBOARD_YOUTUBE_OAUTH_MODE": "unexpected"}
            )

    def test_service_builder_receives_credentials_and_options(self):
        credentials = object()
        service = object()
        credentials_getter = mock.Mock(return_value=credentials)
        build_factory = mock.Mock(return_value=service)

        result = youtube.get_youtube_service(
            self.settings,
            True,
            credentials_getter=credentials_getter,
            build_factory=build_factory,
        )

        self.assertIs(result, service)
        credentials_getter.assert_called_once_with(
            self.settings, interactive=True
        )
        build_factory.assert_called_once_with(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    def test_disconnected_and_connected_status_contracts(self):
        candidates = [self.fixed_secret, self.app_dir / "client_secret.json"]
        disconnected = youtube.youtube_status(
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            secret_candidates=candidates,
            libraries_available=False,
        )
        self.assertEqual(
            disconnected,
            {
                "google_libs_available": False,
                "client_secret_exists": False,
                "client_secret_path": str(self.fixed_secret),
                "client_secret_candidates": [str(path) for path in candidates],
                "token_exists": False,
                "token_path": str(self.fixed_token),
                "connected": False,
                "channel_title": "",
                "error": "",
            },
        )

        self.fixed_secret.parent.mkdir(parents=True)
        self.fixed_secret.write_text("{}", encoding="utf-8")
        self.fixed_token.write_text("{}", encoding="utf-8")
        service = mock.Mock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "Test Channel"}}]
        }
        service_getter = mock.Mock(return_value=service)
        connected = youtube.youtube_status(
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            secret_candidates=candidates,
            libraries_available=True,
            service_getter=service_getter,
        )
        self.assertTrue(connected["connected"])
        self.assertEqual(connected["channel_title"], "Test Channel")
        self.assertEqual(connected["error"], "")
        service_getter.assert_called_once_with(
            self.settings, interactive=False
        )

    def test_status_captures_service_failure_without_raising(self):
        self.fixed_token.parent.mkdir(parents=True)
        self.fixed_token.write_text("{}", encoding="utf-8")
        status_payload = youtube.youtube_status(
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            secret_candidates=[],
            libraries_available=True,
            service_getter=mock.Mock(
                side_effect=RuntimeError("channel lookup failed")
            ),
        )
        self.assertFalse(status_payload["connected"])
        self.assertEqual(
            status_payload["error"],
            "RuntimeError: channel lookup failed",
        )

    def test_playlist_listing_paginates_and_preserves_order(self):
        service = mock.Mock()
        service.playlists.return_value.list.return_value.execute.side_effect = [
            {
                "items": [
                    {"id": "one", "snippet": {"title": "First"}},
                    {"id": "two", "snippet": {}},
                ],
                "nextPageToken": "next",
            },
            {
                "items": [
                    {"id": "three", "snippet": {"title": "Third"}}
                ]
            },
        ]
        service_getter = mock.Mock(return_value=service)

        result = youtube.list_youtube_playlists(
            self.settings, service_getter=service_getter
        )

        self.assertEqual(
            result,
            [
                {"id": "one", "title": "First"},
                {"id": "two", "title": "Untitled"},
                {"id": "three", "title": "Third"},
            ],
        )
        self.assertEqual(
            service.playlists.return_value.list.call_args_list,
            [
                mock.call(
                    part="snippet",
                    mine=True,
                    maxResults=50,
                    pageToken=None,
                ),
                mock.call(
                    part="snippet",
                    mine=True,
                    maxResults=50,
                    pageToken="next",
                ),
            ],
        )

    def test_playlist_and_connection_errors_remain_visible(self):
        for error in (
            youtube.YouTubeNotConnectedError("not connected"),
            RuntimeError("playlist API failed"),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    youtube.list_youtube_playlists(
                        self.settings,
                        service_getter=mock.Mock(side_effect=error),
                    )

    def test_connect_error_payload_preserves_hints_and_file_state(self):
        payload = youtube.youtube_connect_error_payload(
            RuntimeError("redirect_uri_mismatch"),
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            libraries_available=True,
        )
        self.assertEqual(payload["ok"], False)
        self.assertEqual(
            payload["error"], "RuntimeError: redirect_uri_mismatch"
        )
        self.assertIn("client_secret.json is missing", payload["hint"])
        self.assertEqual(payload["client_secret_exists"], False)
        self.assertEqual(payload["token_exists"], False)
        self.assertEqual(payload["google_libs_available"], True)

    def test_external_bootstrap_error_is_actionable_and_sanitized(self):
        self.fixed_secret.parent.mkdir(parents=True)
        self.fixed_secret.write_text("{}", encoding="utf-8")
        payload = youtube.youtube_connect_error_payload(
            youtube.YouTubeOAuthBootstrapRequiredError(
                "Interactive YouTube OAuth is disabled in this deployment."
            ),
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            libraries_available=True,
        )
        self.assertIn("disabled in this deployment", payload["error"])
        self.assertIn("python -m vod_dashboard.youtube_oauth", payload["hint"])

        secret_value = "never-expose-this-oauth-value"
        sanitized = youtube.youtube_connect_error_payload(
            RuntimeError(secret_value),
            self.settings,
            secret_path=self.fixed_secret,
            token_path=self.fixed_token,
            libraries_available=True,
        )
        self.assertNotIn(secret_value, str(sanitized))


class YouTubeUploadHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.media_root.mkdir()
        self.video = self.media_root / "Example" / "vod.mp4"
        self.video.parent.mkdir()
        self.video.write_bytes(b"video")
        self.policy = MediaPathPolicy(self.media_root)
        self.settings = {
            "download_path": str(self.media_root),
            "youtube_upload_mode": "stable",
            "youtube_chunk_size_mb": 64,
            "youtube_privacy_status": "unlisted",
            "youtube_tags": "twitch, archive",
            "youtube_category_id": "20",
            "youtube_playlist_id": "",
            "move_uploaded_vods": True,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def metadata():
        return {
            "title": "Example VOD",
            "description": "Example description",
            "meta": {
                "streamer": "Example",
                "date_de": "04.03.2026",
                "vod_id": "1234567890",
            },
        }

    def upload(self, service, **overrides):
        dependencies = {
            "media_policy": self.policy,
            "service_getter": mock.Mock(return_value=service),
            "metadata_builder": mock.Mock(return_value=self.metadata()),
            "media_upload_factory": mock.Mock(return_value=object()),
            "history_recorder": mock.Mock(),
            "move_after_upload": mock.Mock(return_value=self.video),
            "job_log_callback": mock.Mock(),
        }
        dependencies.update(overrides)
        result = youtube.upload_video_to_youtube(
            self.video,
            self.settings,
            **dependencies,
        )
        return result, dependencies

    def test_chunk_size_normalization_and_mode_labels(self):
        cases = (
            ({}, 64, "Stable"),
            ({"youtube_upload_mode": "safe"}, 32, "Very Stable"),
            ({"youtube_upload_mode": "fast"}, 128, "Fast"),
            (
                {
                    "youtube_upload_mode": "manual",
                    "youtube_chunk_size_mb": "17",
                },
                17,
                "Manual (17 MB)",
            ),
            (
                {
                    "youtube_upload_mode": "manual",
                    "youtube_chunk_size_mb": 0,
                },
                1,
                "Manual (1 MB)",
            ),
            (
                {
                    "youtube_upload_mode": "manual",
                    "youtube_chunk_size_mb": "invalid",
                },
                64,
                "Manual (64 MB)",
            ),
            ({"youtube_upload_mode": "unknown"}, 64, "Stable"),
        )
        for settings, expected_chunk, expected_label in cases:
            with self.subTest(settings=settings):
                self.assertEqual(
                    youtube.youtube_chunk_mb(settings), expected_chunk
                )
                self.assertEqual(
                    youtube.youtube_mode_label(settings), expected_label
                )

    def test_successful_upload_body_privacy_and_resumable_media(self):
        service = mock.Mock()
        upload_request = service.videos.return_value.insert.return_value
        upload_request.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )
        media = object()
        media_factory = mock.Mock(return_value=media)
        metadata_builder = mock.Mock(return_value=self.metadata())

        video_id, dependencies = self.upload(
            service,
            media_upload_factory=media_factory,
            metadata_builder=metadata_builder,
        )

        self.assertEqual(video_id, "youtube-video-1")
        media_factory.assert_called_once_with(
            str(self.video.resolve()),
            mimetype="video/mp4",
            chunksize=64 * 1024 * 1024,
            resumable=True,
        )
        insert_call = service.videos.return_value.insert.call_args
        self.assertEqual(insert_call.kwargs["part"], "snippet,status")
        self.assertIs(insert_call.kwargs["media_body"], media)
        self.assertEqual(
            insert_call.kwargs["body"],
            {
                "snippet": {
                    "title": "Example VOD",
                    "description": "Example description",
                    "tags": ["twitch", "archive"],
                    "categoryId": "20",
                },
                "status": {
                    "privacyStatus": "unlisted",
                    "selfDeclaredMadeForKids": False,
                },
            },
        )
        dependencies["history_recorder"].assert_called_once_with(
            self.video.resolve()
        )

    def test_upload_sanitizes_title_at_the_api_boundary(self):
        service = mock.Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )
        metadata = self.metadata()
        metadata["title"] = "A << > B"

        self.upload(
            service,
            metadata_builder=mock.Mock(return_value=metadata),
        )

        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "A B")
        self.assertEqual(metadata["title"], "A << > B")

    def test_invalid_privacy_falls_back_to_private(self):
        self.settings["youtube_privacy_status"] = "friends"
        service = mock.Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )

        self.upload(service)

        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "private")

    def test_progress_uses_next_chunk_until_response(self):
        service = mock.Mock()
        status = SimpleNamespace(
            resumable_progress=427_000,
            total_size=1_000_000,
            progress=lambda: 0.427,
        )
        upload_request = service.videos.return_value.insert.return_value
        upload_request.next_chunk.side_effect = [
            (status, None),
            (None, {"id": "youtube-video-1"}),
        ]
        logger = mock.Mock()
        progress_callback = mock.Mock()

        result, _ = self.upload(
            service,
            job_id="job-1",
            job_log_callback=logger,
            progress_callback=progress_callback,
        )

        self.assertEqual(result, "youtube-video-1")
        self.assertEqual(upload_request.next_chunk.call_count, 2)
        progress_callback.assert_called_once_with(427_000, 1_000_000)
        self.assertIn(
            mock.call("job-1", "YouTube Upload vod.mp4: 42%"),
            logger.call_args_list,
        )
        self.assertIn(
            mock.call(
                "job-1",
                "YouTube Upload completed: "
                "https://www.youtube.com/watch?v=youtube-video-1",
            ),
            logger.call_args_list,
        )

    def test_upload_api_failure_propagates_without_retry_or_side_effects(self):
        service = mock.Mock()
        upload_request = service.videos.return_value.insert.return_value
        upload_request.next_chunk.side_effect = RuntimeError("upload failed")
        history = mock.Mock()
        move = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            self.upload(
                service,
                history_recorder=history,
                move_after_upload=move,
            )

        upload_request.next_chunk.assert_called_once_with()
        history.assert_not_called()
        move.assert_not_called()
        service.playlistItems.assert_not_called()

    def test_playlist_and_post_upload_operation_order(self):
        self.settings["youtube_playlist_id"] = "playlist-123"
        service = mock.Mock()
        events = []
        upload_request = service.videos.return_value.insert.return_value

        def finish_upload():
            events.append("upload_complete")
            return None, {"id": "youtube-video-1"}

        upload_request.next_chunk.side_effect = finish_upload
        service.playlistItems.return_value.insert.return_value.execute.side_effect = (
            lambda: events.append("playlist_inserted") or {}
        )
        moved = self.video.with_name("moved.mp4")

        def remember(path):
            events.append(f"history:{Path(path).name}")

        def move(path, settings, job_id=None):
            events.append("move")
            return moved

        video_id, _ = self.upload(
            service,
            history_recorder=remember,
            move_after_upload=move,
        )

        self.assertEqual(video_id, "youtube-video-1")
        self.assertEqual(
            events,
            [
                "upload_complete",
                "history:vod.mp4",
                "playlist_inserted",
                "move",
                "history:moved.mp4",
            ],
        )

    def test_playlist_failure_is_logged_and_does_not_block_move(self):
        self.settings["youtube_playlist_id"] = "playlist-123"
        service = mock.Mock()
        events = []
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )
        service.playlistItems.return_value.insert.return_value.execute.side_effect = (
            lambda: events.append("playlist_attempt")
            or (_ for _ in ()).throw(RuntimeError("playlist failed"))
        )

        def remember(path):
            events.append(f"history:{Path(path).name}")

        def move(path, settings, job_id=None):
            events.append("move")
            return path

        logger = mock.Mock()
        video_id, _ = self.upload(
            service,
            job_id="job-1",
            history_recorder=remember,
            move_after_upload=move,
            job_log_callback=logger,
        )

        self.assertEqual(video_id, "youtube-video-1")
        self.assertEqual(
            events,
            ["history:vod.mp4", "playlist_attempt", "move"],
        )
        self.assertIn(
            mock.call(
                "job-1",
                "Could not add VOD to playlist: playlist failed",
            ),
            logger.call_args_list,
        )

    def test_no_playlist_selected_skips_playlist_api(self):
        service = mock.Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {"id": "youtube-video-1"},
        )

        video_id, _ = self.upload(service)

        self.assertEqual(video_id, "youtube-video-1")
        service.playlistItems.assert_not_called()

    def test_missing_video_id_skips_all_post_upload_side_effects(self):
        service = mock.Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None,
            {},
        )
        history = mock.Mock()
        move = mock.Mock()

        video_id, _ = self.upload(
            service,
            history_recorder=history,
            move_after_upload=move,
        )

        self.assertIsNone(video_id)
        history.assert_not_called()
        move.assert_not_called()
        service.playlistItems.assert_not_called()

    def test_uploaded_history_persistence_deduplication_and_no_truncation(self):
        settings_file = self.base / "settings.json"
        existing = ["older.mp4", str(self.video.resolve())]
        youtube.remember_youtube_uploaded_file(
            self.video.resolve(),
            settings_loader=lambda: {
                "youtube_uploaded_files": existing.copy()
            },
            settings_file=settings_file,
            now=lambda: datetime(2026, 8, 19, 12, 0, 0),
        )
        persisted = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["youtube_uploaded_files"], existing)
        self.assertEqual(
            persisted["youtube_upload_history"],
            [
                {
                    "path": str(self.video.resolve()),
                    "uploaded_at": "2026-08-19T12:00:00",
                }
            ],
        )

        long_history = [f"video-{index}.mp4" for index in range(1001)]
        youtube.remember_youtube_uploaded_file(
            self.video.resolve(),
            settings_loader=lambda: {
                "youtube_uploaded_files": long_history.copy()
            },
            settings_file=settings_file,
            now=lambda: datetime(2026, 8, 19, 12, 1, 0),
        )
        persisted = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["youtube_uploaded_files"]), 1002)
        self.assertEqual(
            persisted["youtube_uploaded_files"][-1],
            str(self.video.resolve()),
        )
        self.assertIn("video-0.mp4", persisted["youtube_uploaded_files"])
        self.assertIn("video-1.mp4", persisted["youtube_uploaded_files"])

    def test_move_after_upload_disabled_and_enabled_contracts(self):
        move_bundle = mock.Mock()
        disabled = {
            **self.settings,
            "move_uploaded_vods": False,
        }
        self.assertEqual(
            youtube.move_uploaded_vod_to_done_folder(
                self.video,
                disabled,
                media_policy=self.policy,
                move_bundle=move_bundle,
            ),
            self.video,
        )
        move_bundle.assert_not_called()

        moved = self.video.with_name("moved.mp4")
        move_bundle.return_value = {"new_path": str(moved)}
        self.assertEqual(
            youtube.move_uploaded_vod_to_done_folder(
                self.video,
                self.settings,
                "job-1",
                media_policy=self.policy,
                move_bundle=move_bundle,
            ),
            moved,
        )
        move_bundle.assert_called_once_with(
            self.video.resolve(), self.settings, job_id="job-1"
        )

    def test_move_failure_returns_source_and_preserves_log_message(self):
        logger = mock.Mock()
        result = youtube.move_uploaded_vod_to_done_folder(
            self.video,
            self.settings,
            "job-1",
            media_policy=self.policy,
            move_bundle=mock.Mock(
                side_effect=RuntimeError("move failed")
            ),
            job_log_callback=logger,
        )
        self.assertEqual(result, self.video.resolve())
        logger.assert_called_once_with(
            "job-1", "Move after upload failed: move failed"
        )

    def test_outside_root_upload_is_rejected_before_dependencies(self):
        outside = self.base / "outside.mp4"
        outside.write_bytes(b"outside")
        service_getter = mock.Mock()
        media_factory = mock.Mock()
        history = mock.Mock()
        move = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "outside"):
            youtube.upload_video_to_youtube(
                outside,
                self.settings,
                media_policy=self.policy,
                service_getter=service_getter,
                metadata_builder=mock.Mock(),
                media_upload_factory=media_factory,
                history_recorder=history,
                move_after_upload=move,
            )

        service_getter.assert_not_called()
        media_factory.assert_not_called()
        history.assert_not_called()
        move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
