import ast
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vod_dashboard import settings as settings_helpers
from vod_dashboard import twitch


class PureTwitchHelperTests(unittest.TestCase):
    def test_module_boundary_has_only_standard_library_integration_imports(self):
        module_path = Path(twitch.__file__).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        self.assertEqual(
            imported_roots,
            {
                "__future__",
                "concurrent",
                "datetime",
                "json",
                "pathlib",
                "re",
                "subprocess",
                "sys",
                "tempfile",
                "typing",
                "urllib",
            },
        )
        source = module_path.read_text(encoding="utf-8")
        self.assertNotIn("import app", source)
        self.assertNotIn("flask", source.lower())
        self.assertNotIn("os.environ", source)

    def test_date_parsing_and_entry_date_precedence_are_preserved(self):
        self.assertEqual(twitch.parse_date(" 2026-08-10 "), datetime(2026, 8, 10))
        self.assertEqual(twitch.parse_date("20260810"), datetime(2026, 8, 10))
        self.assertEqual(twitch.parse_date("10.08.2026"), datetime(2026, 8, 10))
        self.assertIsNone(twitch.parse_date("2026/08/10"))
        self.assertIsNone(twitch.parse_date(""))
        self.assertIsNone(twitch.parse_date(None))

        self.assertEqual(
            twitch.entry_date(
                {
                    "upload_date": "20260810",
                    "release_date": "20260809",
                    "timestamp": "1",
                }
            ),
            "2026-08-10",
        )
        self.assertEqual(
            twitch.entry_date(
                {
                    "upload_date": "invalid",
                    "release_date": "09.08.2026",
                }
            ),
            "2026-08-09",
        )
        self.assertEqual(twitch.entry_date({"timestamp": "0"}), "1970-01-01")
        self.assertIsNone(twitch.entry_date({"timestamp": 0}))
        self.assertEqual(
            twitch.entry_date({"release_timestamp": 1_787_507_539}),
            "2026-08-23",
        )

    def test_vod_id_extraction_and_canonical_urls_are_preserved(self):
        self.assertEqual(twitch.vod_id_from_url("videos/123456"), "123456")
        self.assertEqual(twitch.vod_id_from_url("?video=234567"), "234567")
        self.assertEqual(twitch.vod_id_from_url("?v=345678"), "345678")
        self.assertEqual(twitch.vod_id_from_url("reference 12345678 end"), "12345678")
        self.assertEqual(twitch.vod_id_from_url("123456"), "")
        self.assertEqual(
            twitch.extract_twitch_vod_id(
                {"id": "invalid", "display_id": "456789"}
            ),
            "456789",
        )
        self.assertEqual(twitch.extract_twitch_vod_id("567890"), "567890")
        self.assertEqual(twitch.extract_twitch_vod_id("v2854443252"), "2854443252")
        self.assertEqual(twitch.vod_id_from_url("v2854443252"), "2854443252")
        self.assertEqual(twitch.extract_twitch_vod_id("12345"), "")
        self.assertEqual(
            twitch.canonical_twitch_vod_url(
                "https://www.twitch.tv/videos/678901?filter=archives"
            ),
            "https://www.twitch.tv/videos/678901",
        )
        self.assertEqual(twitch.canonical_twitch_vod_url("not-a-vod"), "")

    def test_vod_url_normalization_and_real_url_rules_are_preserved(self):
        canonical = "https://www.twitch.tv/videos/123456"
        self.assertEqual(twitch.normalize_vod_url({"id": "123456"}), canonical)
        self.assertEqual(twitch.normalize_vod_url({"url": "/videos/123456"}), canonical)
        self.assertEqual(twitch.normalize_vod_url({"url": "videos/123456"}), canonical)
        self.assertEqual(twitch.normalize_vod_url("123456"), canonical)
        self.assertEqual(
            twitch.normalize_vod_url(
                {"id": "123456", "url": "https://example.invalid/video"}
            ),
            canonical,
        )
        self.assertEqual(
            twitch.normalize_vod_url({"url": "https://www.twitch.tv/channel"}),
            "",
        )
        self.assertTrue(twitch.is_real_vod_url(canonical))
        self.assertTrue(twitch.is_real_vod_url("http://twitch.tv/videos/123456"))
        self.assertFalse(twitch.is_real_vod_url("https://m.twitch.tv/videos/123456"))
        self.assertFalse(twitch.is_real_vod_url("https://www.twitch.tv/channel"))

    def test_live_upcoming_and_inclusive_date_range_rules_are_preserved(self):
        for status in (
            "is_live",
            "is_upcoming",
            "is_live_notification",
            "is_upcoming_notification",
        ):
            with self.subTest(status=status):
                self.assertTrue(
                    twitch.is_live_or_upcoming_entry({"live_status": status})
                )
        self.assertTrue(twitch.is_live_or_upcoming_entry({"is_live": True}))
        self.assertTrue(twitch.is_live_or_upcoming_entry({"is_upcoming": True}))
        self.assertTrue(
            twitch.is_live_or_upcoming_entry(
                {"url": "https://www.twitch.tv/example_channel"}
            )
        )
        self.assertFalse(
            twitch.is_live_or_upcoming_entry(
                {
                    "live_status": "was_live",
                    "url": "https://www.twitch.tv/videos/123456",
                }
            )
        )
        self.assertFalse(
            twitch.is_live_or_upcoming_entry(
                {
                    "id": "123456",
                    "url": "https://www.twitch.tv/example_channel",
                }
            )
        )

        start = datetime(2026, 8, 10)
        end = datetime(2026, 8, 20)
        self.assertTrue(twitch.in_range("2026-08-10", start, end))
        self.assertTrue(twitch.in_range("2026-08-20", start, end))
        self.assertFalse(twitch.in_range("2026-08-09", start, end))
        self.assertFalse(twitch.in_range("2026-08-21", start, end))
        self.assertTrue(twitch.in_range("unbekannt", start, end, True))
        self.assertFalse(twitch.in_range("unknown", start, end, False))
        self.assertTrue(twitch.in_range("malformed", start, end, True))

    def test_single_vod_normalization_and_validation_edge_cases_are_preserved(self):
        canonical = "https://www.twitch.tv/videos/123456"
        accepted = (
            "123456",
            "'123456'",
            '"123456"',
            "www.twitch.tv/videos/123456",
            "twitch.tv/videos/123456",
            "https://m.twitch.tv/videos/123456",
            "http://www.twitch.tv/videos/123456",
            "https://www.twitch.tv/?video=123456",
            "https://example.invalid/videos/123456",
            "https://example.invalid/?v=123456",
        )
        for raw in accepted:
            with self.subTest(raw=raw):
                self.assertEqual(twitch.normalize_single_vod_url(raw), canonical)
                self.assertEqual(
                    twitch.validate_single_vod_url(raw),
                    {
                        "ok": True,
                        "url": canonical,
                        "vod_id": "123456",
                    },
                )

        expected_error = {
            "ok": False,
            "error": "Enter a valid Twitch VOD link, for example https://www.twitch.tv/videos/1234567890",
            "url": "",
            "vod_id": "",
        }
        self.assertEqual(twitch.validate_single_vod_url("12345"), expected_error)
        self.assertEqual(twitch.validate_single_vod_url("not-a-vod"), expected_error)


class YtDlpIntegrationHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.download_dir = self.base / "downloads"
        self.archive_path = self.base / "archive.txt"
        self.settings = {
            "cookie_file": "",
            "cookie_browser": "",
            "fragments": 8,
            "quality": "source/best",
            "twitch_rate_limit": "",
            "output_template": settings_helpers.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            "merge_format": "mp4",
            "playlist_end": 150,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_base_ytdlp_command(self):
        self.assertEqual(
            twitch.ytdlp_base_command(), [sys.executable, "-m", "yt_dlp"]
        )
        self.assertEqual(
            twitch.ytdlp_base_command("custom-python"),
            ["custom-python", "-m", "yt_dlp"],
        )

    def test_cookie_file_browser_fallback_none_and_precedence(self):
        cookie_file = self.base / "cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n", encoding="utf-8"
        )
        self.assertEqual(
            twitch.ytdlp_cookie_args(
                {
                    "cookie_file": str(cookie_file),
                    "cookie_browser": "firefox",
                }
            ),
            ["--cookies", str(cookie_file)],
        )
        self.assertEqual(
            twitch.ytdlp_cookie_args(
                {"cookie_file": "", "cookie_browser": "firefox"}
            ),
            ["--cookies-from-browser", "firefox"],
        )
        self.assertEqual(
            twitch.ytdlp_cookie_args(
                {"cookie_file": "", "cookie_browser": ""}
            ),
            [],
        )
        with self.assertRaisesRegex(RuntimeError, "Cookie file not found"):
            twitch.ytdlp_cookie_args(
                {
                    "cookie_file": str(self.base / "missing.txt"),
                    "cookie_browser": "firefox",
                }
            )

    def test_fresh_default_settings_do_not_force_browser_cookie_extraction(self):
        self.assertEqual(settings_helpers.DEFAULT_SETTINGS["cookie_browser"], "")
        self.assertEqual(
            twitch.ytdlp_cookie_args(settings_helpers.DEFAULT_SETTINGS), []
        )

    def test_rate_limit_normalization_contract(self):
        cases = {
            None: "",
            "": "",
            " 5m ": "5M",
            "2.5g": "2.5G",
            "800K": "800K",
            "100": "100",
            "5 MB": "",
            "unlimited": "",
            "-1M": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    twitch.clean_twitch_rate_limit(value), expected
                )

    def test_download_command_order_file_and_safe_template(self):
        configured = {
            **self.settings,
            "fragments": 6,
            "quality": "bestvideo+bestaudio/best",
            "twitch_rate_limit": "5m",
        }
        command, url_file = twitch.build_download_command(
            ["https://www.twitch.tv/videos/1234567890"],
            configured,
            download_directory=self.download_dir,
            archive_path=self.archive_path,
            command_factory=lambda: ["python", "-m", "yt_dlp"],
        )
        try:
            self.assertEqual(
                command,
                [
                    "python",
                    "-m",
                    "yt_dlp",
                    "--ignore-errors",
                    "--downloader",
                    "m3u8:ffmpeg",
                    "--print",
                    "before_dl:VOD-DASHBOARD-DURATION=%(duration)s",
                    "--no-quiet",
                    "-a",
                    str(url_file),
                    "-N",
                    "6",
                    "-f",
                    "bestvideo+bestaudio/best",
                    "--download-archive",
                    str(self.archive_path),
                    "--retries",
                    "infinite",
                    "--fragment-retries",
                    "infinite",
                    "--continue",
                    "--write-info-json",
                    "-P",
                    str(self.download_dir),
                    "-o",
                    settings_helpers.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
                    "--merge-output-format",
                    "mp4",
                    "--limit-rate",
                    "5M",
                ],
            )
            self.assertEqual(command.count("--downloader"), 1)
            self.assertEqual(
                command[command.index("--downloader") + 1],
                "m3u8:ffmpeg",
            )
            self.assertEqual(command.count("--print"), 1)
            self.assertEqual(
                command[command.index("--print") + 1],
                "before_dl:VOD-DASHBOARD-DURATION=%(duration)s",
            )
            self.assertEqual(
                url_file.read_text(encoding="utf-8"),
                "https://www.twitch.tv/videos/1234567890\n",
            )
        finally:
            url_file.unlink(missing_ok=True)

    def test_unsafe_output_template_uses_existing_settings_repair(self):
        configured = settings_helpers.fix_template_confusion(
            {
                **self.settings,
                "output_template": "../../outside/%(id)s.%(ext)s",
                "manual_upload_filename_template": "{date_de} - {title}",
            }
        )
        command, url_file = twitch.build_download_command(
            [],
            configured,
            download_directory=self.download_dir,
            archive_path=self.archive_path,
            command_factory=lambda: ["python", "-m", "yt_dlp"],
        )
        try:
            self.assertEqual(
                command[command.index("-o") + 1],
                settings_helpers.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            )
            self.assertEqual(url_file.read_text(encoding="utf-8"), "\n")
        finally:
            url_file.unlink(missing_ok=True)

    def test_vod_detail_subprocess_success_empty_and_failure(self):
        success = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "1234567890", "title": "VOD"}),
            stderr="",
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=success
        ) as run:
            result = twitch.run_ytdlp_vod_detail(
                "https://www.twitch.tv/videos/1234567890",
                self.settings,
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )
        self.assertEqual(result, {"id": "1234567890", "title": "VOD"})
        run.assert_called_once_with(
            [
                "python",
                "-m",
                "yt_dlp",
                "--dump-single-json",
                "--no-playlist",
                "https://www.twitch.tv/videos/1234567890",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        empty = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=empty
        ):
            self.assertEqual(
                twitch.run_ytdlp_vod_detail("vod", self.settings), {}
            )

        failure = SimpleNamespace(
            returncode=1, stdout="fallback stdout", stderr="detail failed"
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=failure
        ):
            with self.assertRaisesRegex(RuntimeError, "detail failed"):
                twitch.run_ytdlp_vod_detail("vod", self.settings)

    def test_vod_detail_reuses_cookie_auth_and_accepts_warning_on_success(self):
        cookie_file = self.base / "subscriber-cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n", encoding="utf-8"
        )
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "2854443252",
                    "upload_date": "20260823",
                    "timestamp": 1_787_507_539,
                }
            ),
            stderr=(
                "WARNING: Unable to download JSON metadata: "
                "HTTP Error 403: Forbidden"
            ),
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=process
        ) as run:
            result = twitch.run_ytdlp_vod_detail(
                "https://www.twitch.tv/videos/2854443252",
                {"cookie_file": str(cookie_file), "cookie_browser": "firefox"},
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )

        self.assertEqual(twitch.entry_date(result), "2026-08-23")
        command = run.call_args.args[0]
        self.assertIn("--cookies", command)
        self.assertEqual(command[command.index("--cookies") + 1], str(cookie_file))
        self.assertNotIn("--cookies-from-browser", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_live_status_command_cookies_and_safe_metadata_normalization(self):
        cookie_file = self.base / "cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n", encoding="utf-8"
        )
        metadata = {
            "id": "987654321",
            "title": "Nika (live)",
            "description": "Mein echter Streamtitel",
            "uploader": "Nika LiveTV",
            "uploader_id": "Nika_LiveTV",
            "timestamp": 1_700_000_000,
            "is_live": True,
            "live_status": "is_live",
            "formats": [
                {
                    "format_id": "Source",
                    "format_note": "Source",
                    "height": 1080,
                    "fps": 60,
                    "url": "https://signed.invalid/master.m3u8?token=SECRET",
                    "http_headers": {"Authorization": "SECRET"},
                },
                {"format_id": "1080p60", "height": 1080, "fps": 60},
                {"format_id": "1080p60 duplicate", "height": 1080, "fps": 60},
                {"format_id": "720p60", "height": 720, "fps": 60},
                {"format_id": "720p", "height": 720, "fps": 30},
                {"format_id": "480p", "height": 480, "fps": 60},
                {"format_id": "audio_only", "vcodec": "none"},
                {"format_id": "hls-technical"},
            ],
            "manifest_url": "https://signed.invalid/SECRET",
            "token": "SECRET",
        }
        success = SimpleNamespace(
            returncode=0, stdout=json.dumps(metadata), stderr=""
        )
        settings = {**self.settings, "cookie_file": str(cookie_file)}

        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=success
        ) as run:
            result = twitch.run_ytdlp_live_status(
                "@Nika_LiveTV",
                settings,
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )

        self.assertEqual(
            result,
            {
                "streamer": "nika_livetv",
                "state": "live",
                "display_name": "Nika LiveTV",
                "stream_id": "987654321",
                "title": "Mein echter Streamtitel",
                "started_at": "2023-11-14T22:13:20Z",
                "qualities": [
                    "Source",
                    "1080p60",
                    "720p60",
                    "720p",
                    "480p60",
                ],
            },
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "python",
                "-m",
                "yt_dlp",
                "--dump-single-json",
                "--skip-download",
                "--no-playlist",
                "--cookies",
                str(cookie_file),
                "https://www.twitch.tv/nika_livetv",
            ],
        )
        self.assertEqual(
            run.call_args.kwargs,
            {
                "capture_output": True,
                "text": True,
                "timeout": twitch.LIVE_STATUS_TIMEOUT_SECONDS,
            },
        )
        for forbidden in (
            "--download-archive",
            "--write-info-json",
            "--downloader",
            "-P",
            "-o",
        ):
            self.assertNotIn(forbidden, command)
        serialized = json.dumps(result)
        for secret in (
            "url",
            "manifest_url",
            "http_headers",
            "cookies",
            "token",
            "SECRET",
        ):
            self.assertNotIn(secret, serialized)

    def test_live_status_offline_is_narrow_and_other_failures_remain_errors(self):
        offline = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "ERROR: [twitch:stream] nika_livetv: "
                "The channel is not currently live"
            ),
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=offline
        ):
            self.assertEqual(
                twitch.run_ytdlp_live_status("Nika_LiveTV", self.settings),
                {"streamer": "nika_livetv", "state": "offline"},
            )

        network_failure = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ERROR: unable to download Twitch GraphQL metadata",
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=network_failure
        ):
            with self.assertRaisesRegex(RuntimeError, "failed with code 1"):
                twitch.run_ytdlp_live_status("nika_livetv", self.settings)

    def test_live_status_timeout_is_short_and_remains_an_error(self):
        self.assertEqual(twitch.LIVE_STATUS_TIMEOUT_SECONDS, 30)
        self.assertLess(twitch.LIVE_STATUS_TIMEOUT_SECONDS, 180)

        with mock.patch(
            "vod_dashboard.twitch.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["yt-dlp"], 30),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                twitch.run_ytdlp_live_status("nika_livetv", self.settings)

    def test_live_status_start_time_fallback_missing_and_title_fallback(self):
        cases = (
            (
                {
                    "id": "1",
                    "title": "Fallback title",
                    "description": "",
                    "upload_date": "20260823",
                    "is_live": True,
                },
                "2026-08-23",
                "Fallback title",
            ),
            (
                {
                    "id": "2",
                    "title": "No date",
                    "timestamp": "invalid",
                    "upload_date": "invalid",
                    "is_live": True,
                },
                None,
                "No date",
            ),
        )
        for metadata, expected_start, expected_title in cases:
            with self.subTest(metadata=metadata), mock.patch(
                "vod_dashboard.twitch.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(metadata), stderr=""
                ),
            ):
                result = twitch.run_ytdlp_live_status(
                    "nika_livetv", self.settings
                )
            self.assertEqual(result["started_at"], expected_start)
            self.assertEqual(result["title"], expected_title)

    def test_live_status_rejects_invalid_login_and_indefinite_metadata(self):
        with self.assertRaisesRegex(ValueError, "valid Twitch streamer"):
            twitch.run_ytdlp_live_status("bad-name!", self.settings)

        ambiguous = SimpleNamespace(
            returncode=0, stdout=json.dumps({"id": "1"}), stderr=""
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run", return_value=ambiguous
        ):
            with self.assertRaisesRegex(RuntimeError, "definitive live state"):
                twitch.run_ytdlp_live_status("nika_livetv", self.settings)

    def test_live_recording_command_is_single_channel_safe_and_non_batch(self):
        cookie_file = self.base / "recording-cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\nSECRET-COOKIE", encoding="utf-8"
        )
        configured = {
            **self.settings,
            "cookie_file": str(cookie_file),
            "quality": "1080p60/source/best",
            "merge_format": "mp4",
        }

        command = twitch.build_live_recording_command(
            "nika_livetv",
            configured,
            download_directory=self.download_dir,
            command_factory=lambda: ["python", "-m", "yt_dlp"],
        )

        self.assertEqual(
            command,
            [
                "python",
                "-m",
                "yt_dlp",
                "--cookies",
                str(cookie_file),
                "--no-playlist",
                "--downloader",
                "m3u8:ffmpeg",
                "-f",
                "1080p60/source/best",
                "--write-info-json",
                "--print",
                (
                    "after_move:VOD-DASHBOARD-RECORDING-FILE="
                    "%(filepath)s"
                ),
                "--no-quiet",
                "-P",
                str(self.download_dir.resolve()),
                "-o",
                (
                    "nika_livetv/%(upload_date)s - %(uploader)s - LIVE - "
                    "%(title)s [%(id)s].%(ext)s"
                ),
                "--merge-output-format",
                "mp4",
                "https://www.twitch.tv/nika_livetv",
            ],
        )
        self.assertEqual(command.count("--downloader"), 1)
        for forbidden in (
            "--download-archive",
            "-a",
            "--ignore-errors",
            "--retries",
            "--fragment-retries",
            "--continue",
        ):
            self.assertNotIn(forbidden, command)
        self.assertNotIn("SECRET-COOKIE", " ".join(command))

    def test_live_recording_command_requires_a_normalized_login(self):
        for invalid in ("@nika_livetv", "Nika_LiveTV", "bad-name!"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "normalized Twitch streamer"
            ):
                twitch.build_live_recording_command(
                    invalid,
                    self.settings,
                    download_directory=self.download_dir,
                )

    def test_live_recording_retry_uses_distinct_deterministic_output(self):
        first = twitch.live_recording_output_template("nika_livetv")
        retry = twitch.live_recording_output_template(
            "nika_livetv", attempt=2
        )
        self.assertNotEqual(first, retry)
        self.assertNotIn("RETRY", first)
        self.assertIn(" - RETRY 2.%(ext)s", retry)

        command = twitch.build_live_recording_command(
            "nika_livetv",
            self.settings,
            attempt=2,
            download_directory=self.download_dir,
        )
        self.assertEqual(command[command.index("-o") + 1], retry)

    def test_source_fallback_deduplication_and_malformed_output(self):
        first = SimpleNamespace(
            returncode=1, stdout="", stderr="first source failed"
        )
        second = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "entries": [
                        {
                            "id": "1234567890",
                            "title": "First",
                            "url": "https://www.twitch.tv/videos/1234567890",
                        },
                        {
                            "id": "1234567890",
                            "title": "Duplicate",
                        },
                        {
                            "display_id": "2345678901",
                            "title": "Second",
                        },
                        "not-a-dict",
                    ]
                }
            ),
        )
        malformed = SimpleNamespace(
            returncode=0, stdout="{not-json", stderr=""
        )
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run",
            side_effect=[first, second, malformed],
        ) as run:
            playlists = twitch.run_ytdlp_json_sources(
                "@example_streamer",
                25,
                self.settings,
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )

        self.assertEqual(len(playlists), 3)
        self.assertEqual(playlists[0]["_returncode"], 1)
        self.assertEqual(playlists[0]["_stderr"], "first source failed")
        self.assertEqual(playlists[0]["entries"], [])
        self.assertEqual(
            [entry["id"] for entry in playlists[1]["entries"]],
            ["1234567890", "2345678901"],
        )
        self.assertEqual(playlists[2]["_returncode"], -999)
        self.assertIn("Expecting property name", playlists[2]["_stderr"])
        expected_urls = [
            "https://www.twitch.tv/example_streamer/videos?filter=archives&sort=time",
            "https://www.twitch.tv/example_streamer/videos?filter=all&sort=time",
            "https://www.twitch.tv/example_streamer/videos",
        ]
        self.assertEqual(
            [playlist["_source_url"] for playlist in playlists], expected_urls
        )
        self.assertEqual(run.call_count, 3)
        for call, expected_url in zip(run.call_args_list, expected_urls):
            command = call.args[0]
            self.assertEqual(command[-1], expected_url)
            self.assertEqual(command[command.index("--playlist-end") + 1], "25")
            self.assertEqual(
                call.kwargs,
                {"capture_output": True, "text": True, "timeout": 180},
            )

    def test_source_legacy_argument_order_and_settings_loader(self):
        empty = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run",
            side_effect=[empty, empty, empty],
        ) as run:
            twitch.run_ytdlp_json_sources(
                "streamer",
                self.settings,
                7,
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("--playlist-end") + 1], "7")

        loader = mock.Mock(return_value=self.settings)
        with mock.patch(
            "vod_dashboard.twitch.subprocess.run",
            side_effect=[empty, empty, empty],
        ):
            twitch.run_ytdlp_json_sources(
                "streamer",
                settings_loader=loader,
                command_factory=lambda: ["python", "-m", "yt_dlp"],
            )
        loader.assert_called_once_with()

    def test_run_ytdlp_json_for_streamer_compatibility_shape(self):
        playlists = [
            {"entries": [{"id": "1"}, "ignored"]},
            "ignored-playlist",
            {"entries": [{"id": "2"}]},
        ]
        source_runner = mock.Mock(return_value=playlists)

        result = twitch.run_ytdlp_json_for_streamer(
            "ExampleStreamer",
            self.settings,
            33,
            source_runner=source_runner,
        )

        source_runner.assert_called_once_with(
            "ExampleStreamer", 33, self.settings
        )
        self.assertEqual(
            result,
            {
                "id": "ExampleStreamer",
                "title": "ExampleStreamer",
                "entries": [{"id": "1"}, {"id": "2"}],
                "_debug_sources": playlists,
            },
        )


if __name__ == "__main__":
    unittest.main()
