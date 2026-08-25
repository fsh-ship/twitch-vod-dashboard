import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from vod_dashboard import settings
from vod_dashboard.media import MediaPathPolicy


DEFAULT_SETTINGS_KEYS = {
    "archive_file",
    "auto_recorder_enabled",
    "auto_vod_enabled",
    "auto_youtube_enabled",
    "auto_vod_poll_minutes",
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


class SettingsRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.runtime_dir = self.base / "data"
        self.settings_file = self.runtime_dir / "dashboard-settings.json"
        self.media_root.mkdir()
        self.runtime_dir.mkdir()
        self.environ = {}
        self.defaults = {
            **settings.DEFAULT_SETTINGS,
            "download_path": str(self.media_root),
            "streamer_file": str(self.runtime_dir / "streamer.txt"),
            "archive_file": str(self.runtime_dir / "archive.txt"),
            "youtube_client_secret_file": str(
                self.runtime_dir / "client_secret.json"
            ),
            "youtube_token_file": str(self.runtime_dir / "youtube-token.json"),
            "uploaded_vods_folder": str(self.media_root / "_hochgeladen"),
            "youtube_uploaded_files": [],
        }
        self.ensure_calls = []
        self.repository = settings.SettingsRepository(
            settings_file=self.settings_file,
            media_policy=MediaPathPolicy(self.media_root),
            default_settings=self.defaults,
            default_dashboard_dir=self.runtime_dir,
            fixed_streamer_file=self.runtime_dir / "streamer.txt",
            fixed_archive_file=self.runtime_dir / "archive.txt",
            fixed_uploaded_vods_folder=self.media_root / "_hochgeladen",
            environ=self.environ,
            ensure_files=self.ensure_calls.append,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_exact_default_settings_contract(self):
        runtime_paths = settings._MODULE_RUNTIME_PATHS
        expected = {
            "download_path": str(runtime_paths.media_root),
            "streamer_file": str(runtime_paths.streamer_file),
            "archive_file": str(runtime_paths.archive_file),
            "cookie_browser": "",
            "cookie_file": "",
            "quality": "source/best",
            "fragments": 8,
            "twitch_rate_limit": "",
            "playlist_end": 150,
            "include_unknown_dates": True,
            "exclude_live_streams": True,
            "only_real_vod_urls": True,
            "strict_date_filter": False,
            "enrich_vod_dates": True,
            "output_template": settings.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            "merge_format": "mp4",
            "youtube_enabled": False,
            "youtube_auto_upload": False,
            "batch_postprocess_mode": "after_each",
            "youtube_privacy_status": "private",
            "youtube_playlist_id": "",
            "auto_recorder_enabled": False,
            "auto_vod_enabled": False,
            "auto_youtube_enabled": False,
            "auto_vod_poll_minutes": 60,
            "streamer_profiles": {},
            "youtube_client_secret_file": str(
                runtime_paths.youtube_client_secret_file
            ),
            "youtube_token_file": str(runtime_paths.youtube_token_file),
            "youtube_description": "Automatically uploaded by Twitch VOD Dashboard.",
            "youtube_tags": "twitch,vod",
            "youtube_category_id": "20",
            "youtube_chunk_size_mb": 64,
            "youtube_upload_history": [],
            "youtube_uploaded_files": [],
            "move_uploaded_vods": True,
            "uploaded_vods_folder": str(runtime_paths.uploaded_vods_folder),
            "youtube_title_template": "{streamer} VOD - {date_de} - {title}",
            "youtube_description_template": "Automatically archived Twitch VOD.\n\nStreamer: {streamer}\nDate: {date_de}\nOriginal: {url}\nVOD ID: {vod_id}\nDuration: {duration}\n\nPrivate archive.",
            "youtube_upload_mode": "stable",
            "manual_upload_prepare_enabled": True,
            "manual_upload_rename_video": True,
            "manual_upload_filename_template": "{date_de} - {streamer} - {title}",
            "manual_upload_write_description": True,
            "manual_upload_write_metadata_json": True,
        }
        self.assertEqual(set(settings.DEFAULT_SETTINGS), DEFAULT_SETTINGS_KEYS)
        self.assertEqual(len(settings.DEFAULT_SETTINGS), 44)
        self.assertEqual(settings.DEFAULT_SETTINGS, expected)

    def test_legacy_settings_without_automation_fields_use_defaults(self):
        legacy = {
            key: value
            for key, value in self.defaults.items()
            if key not in {
                "auto_recorder_enabled",
                "auto_vod_enabled",
                "auto_youtube_enabled",
                "auto_vod_poll_minutes",
                "streamer_profiles",
            }
        }
        self.settings_file.write_text(
            json.dumps(legacy), encoding="utf-8"
        )

        loaded = self.repository.load()

        self.assertIs(loaded["auto_recorder_enabled"], False)
        self.assertIs(loaded["auto_vod_enabled"], False)
        self.assertIs(loaded["auto_youtube_enabled"], False)
        self.assertEqual(loaded["auto_vod_poll_minutes"], 60)
        self.assertEqual(loaded["streamer_profiles"], {})

    def test_global_auto_recorder_enabled_round_trips(self):
        saved = self.repository.save({"auto_recorder_enabled": True})
        loaded = self.repository.load()
        persisted = json.loads(
            self.settings_file.read_text(encoding="utf-8")
        )

        for payload in (saved, loaded, persisted):
            self.assertIs(payload["auto_recorder_enabled"], True)

    def test_auto_vod_settings_are_strict_and_only_accept_supported_polls(self):
        cases = (
            (True, 60, True, 60),
            (False, 120, False, 120),
            ("true", 30, False, 60),
            (1, 90, False, 60),
            (True, "120", True, 60),
            (True, True, True, 60),
        )
        for enabled, poll, expected_enabled, expected_poll in cases:
            with self.subTest(enabled=enabled, poll=poll):
                normalized = self.repository.normalize(
                    {
                        **self.defaults,
                        "auto_vod_enabled": enabled,
                        "auto_vod_poll_minutes": poll,
                    }
                )
                self.assertIs(normalized["auto_vod_enabled"], expected_enabled)
                self.assertEqual(normalized["auto_vod_poll_minutes"], expected_poll)

    def test_auto_vod_settings_round_trip_without_affecting_auto_recorder(self):
        saved = self.repository.save(
            {
                "auto_recorder_enabled": True,
                "auto_vod_enabled": True,
                "auto_vod_poll_minutes": 120,
            }
        )
        self.assertIs(saved["auto_recorder_enabled"], True)
        self.assertIs(saved["auto_vod_enabled"], True)
        self.assertEqual(saved["auto_vod_poll_minutes"], 120)

    def test_auto_youtube_setting_is_strict_and_independent(self):
        for value, expected in (
            (True, True),
            (False, False),
            ("true", False),
            (1, False),
            (None, False),
        ):
            with self.subTest(value=value):
                normalized = self.repository.normalize(
                    {**self.defaults, "auto_youtube_enabled": value}
                )
                self.assertIs(normalized["auto_youtube_enabled"], expected)

        saved = self.repository.save(
            {"auto_youtube_enabled": True, "youtube_auto_upload": False}
        )
        loaded = self.repository.load()
        self.assertIs(saved["auto_youtube_enabled"], True)
        self.assertIs(loaded["auto_youtube_enabled"], True)
        self.assertIs(saved["youtube_auto_upload"], False)

    def test_streamer_profiles_round_trip_without_touching_streamer_file(self):
        streamer_file = self.runtime_dir / "streamer.txt"
        streamer_file.write_text("Alpha\nBeta\n", encoding="utf-8")

        saved = self.repository.save(
            {
                "streamer_profiles": {
                    "xerax_ttv": {"youtube_playlist_id": "PL123"}
                }
            }
        )
        loaded = self.repository.load()

        expected = {
            "xerax_ttv": {"youtube_playlist_id": "PL123"}
        }
        self.assertEqual(saved["streamer_profiles"], expected)
        self.assertEqual(loaded["streamer_profiles"], expected)
        self.assertEqual(
            streamer_file.read_text(encoding="utf-8"), "Alpha\nBeta\n"
        )

    def test_streamer_profile_keys_and_lookup_are_case_insensitive(self):
        normalized = settings.normalize_streamer_profiles(
            {
                "XeRaX_TTV": {
                    "youtube_playlist_id": " PL123 ",
                    "auto_record": True,
                },
                "xerax_ttv": {"youtube_playlist_id": "PL999"},
                "@XERAX_TTV": {"youtube_playlist_id": "   "},
                "@SECOND_ONE": {"youtube_playlist_id": "PL456"},
            }
        )

        self.assertEqual(
            normalized,
            {
                "xerax_ttv": {
                    "youtube_playlist_id": "PL123",
                    "auto_record": True,
                },
                "second_one": {"youtube_playlist_id": "PL456"},
            },
        )
        profile_settings = {"streamer_profiles": normalized}
        for login in ("xerax_ttv", "XERAX_TTV", "@xerax_ttv"):
            with self.subTest(login=login):
                self.assertEqual(
                    settings.streamer_profile_for(profile_settings, login),
                    {
                        "youtube_playlist_id": "PL123",
                        "auto_record": True,
                    },
                )

    def test_streamer_auto_record_round_trip_and_allowlist(self):
        raw_profiles = {
            "AutoOnly": {"auto_record": True},
            "Combined": {
                "youtube_playlist_id": " PL123 ",
                "auto_record": True,
                "future_field": "drop me",
            },
            "PlaylistOnly": {
                "youtube_playlist_id": "PL456",
                "auto_record": False,
            },
            "FalseOnly": {"auto_record": False},
            "StringTrue": {"auto_record": "true"},
            "NumericTrue": {"auto_record": 1},
        }
        expected = {
            "autoonly": {"auto_record": True},
            "combined": {
                "youtube_playlist_id": "PL123",
                "auto_record": True,
            },
            "playlistonly": {"youtube_playlist_id": "PL456"},
        }

        saved = self.repository.save({"streamer_profiles": raw_profiles})
        loaded = self.repository.load()
        persisted = json.loads(
            self.settings_file.read_text(encoding="utf-8")
        )

        for payload in (saved, loaded, persisted):
            self.assertEqual(payload["streamer_profiles"], expected)

    def test_streamer_auto_vod_profile_field_is_strict_and_independent(self):
        raw_profiles = {
            "VodOnly": {"auto_vod_download": True},
            "AllFields": {
                "youtube_playlist_id": " PL123 ",
                "auto_record": True,
                "auto_vod_download": True,
                "unknown": "discard",
            },
            "FalseOnly": {"auto_vod_download": False},
            "StringTrue": {"auto_vod_download": "true"},
            "NumericTrue": {"auto_vod_download": 1},
        }
        expected = {
            "vodonly": {"auto_vod_download": True},
            "allfields": {
                "youtube_playlist_id": "PL123",
                "auto_record": True,
                "auto_vod_download": True,
            },
        }
        saved = self.repository.save({"streamer_profiles": raw_profiles})
        self.assertEqual(saved["streamer_profiles"], expected)
        self.assertEqual(
            settings.normalize_streamer_profiles(
                {"VodOnly": {"auto_vod_download": False}}
            ),
            {},
        )

    def test_streamer_auto_youtube_profile_field_is_strict_and_compact(self):
        raw_profiles = {
            "YoutubeOnly": {"auto_youtube_upload": True},
            "AllFields": {
                "youtube_playlist_id": " PL123 ",
                "auto_record": True,
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
            "FalseOnly": {"auto_youtube_upload": False},
            "StringTrue": {"auto_youtube_upload": "true"},
            "NumericTrue": {"auto_youtube_upload": 1},
        }
        expected = {
            "youtubeonly": {"auto_youtube_upload": True},
            "allfields": {
                "youtube_playlist_id": "PL123",
                "auto_record": True,
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        }
        saved = self.repository.save({"streamer_profiles": raw_profiles})
        loaded = self.repository.load()
        self.assertEqual(saved["streamer_profiles"], expected)
        self.assertEqual(loaded["streamer_profiles"], expected)

    def test_removing_one_profile_field_preserves_the_other(self):
        playlist_removed = settings.normalize_streamer_profiles(
            {
                "Nika_LiveTV": {
                    "youtube_playlist_id": "",
                    "auto_record": True,
                }
            }
        )
        auto_record_removed = settings.normalize_streamer_profiles(
            {
                "Nika_LiveTV": {
                    "youtube_playlist_id": "PL123",
                    "auto_record": False,
                }
            }
        )

        self.assertEqual(
            playlist_removed,
            {"nika_livetv": {"auto_record": True}},
        )
        self.assertEqual(
            auto_record_removed,
            {"nika_livetv": {"youtube_playlist_id": "PL123"}},
        )

    def test_streamer_profiles_drop_empty_invalid_and_unknown_values(self):
        raw_profiles = {
            "ValidOne": {
                "youtube_playlist_id": "PL123",
                "random_future_setting": "foo",
            },
            "EmptyOne": {"youtube_playlist_id": "   "},
            "invalid-name": {"youtube_playlist_id": "PL456"},
            "": {"youtube_playlist_id": "PL789"},
            "NotAMapping": "PL000",
        }
        normalized = settings.normalize_streamer_profiles(raw_profiles)
        saved = self.repository.save({"streamer_profiles": raw_profiles})
        persisted = json.loads(
            self.settings_file.read_text(encoding="utf-8")
        )

        expected = {"validone": {"youtube_playlist_id": "PL123"}}
        for payload in (
            normalized,
            saved["streamer_profiles"],
            persisted["streamer_profiles"],
        ):
            self.assertEqual(payload, expected)
        self.assertEqual(
            settings.normalize_streamer_profiles(["not", "a", "mapping"]),
            {},
        )
        self.assertEqual(
            settings.streamer_profile_for(
                {"streamer_profiles": normalized}, "invalid-name"
            ),
            {},
        )

    def test_global_playlist_remains_independent_of_streamer_profiles(self):
        saved = self.repository.save(
            {
                "youtube_playlist_id": "GLOBAL-PLAYLIST",
                "streamer_profiles": {
                    "Example": {"youtube_playlist_id": "STREAMER-PLAYLIST"}
                },
            }
        )

        self.assertEqual(saved["youtube_playlist_id"], "GLOBAL-PLAYLIST")
        self.assertEqual(
            settings.streamer_profile_for(saved, "@EXAMPLE"),
            {"youtube_playlist_id": "STREAMER-PLAYLIST"},
        )

    def test_youtube_playlist_resolution_priority_and_fallbacks(self):
        configured = {
            "youtube_playlist_id": "GLOBAL",
            "streamer_profiles": {
                "digitalgirluli": {
                    "youtube_playlist_id": "DIGI"
                }
            },
        }

        cases = (
            ("DigitalGirlUli", None, "DIGI"),
            ("unknown_streamer", None, "GLOBAL"),
            ("", None, "GLOBAL"),
            ("DigitalGirlUli", " SPECIAL ", "SPECIAL"),
            ("DigitalGirlUli", "", ""),
            ("DigitalGirlUli", "   ", ""),
        )
        for streamer, explicit, expected in cases:
            with self.subTest(
                streamer=streamer, explicit=explicit, expected=expected
            ):
                kwargs = (
                    {}
                    if explicit is None
                    else {"explicit_playlist": explicit}
                )
                self.assertEqual(
                    settings.resolve_youtube_playlist_for_streamer(
                        configured, streamer, **kwargs
                    ),
                    expected,
                )

    def test_bool_normalization_including_legacy_values(self):
        truthy = (True, 1, 2.5, "1", "true", "yes", "ja", "on", "an")
        falsy = (False, 0, 0.0, "0", "false", "no", "nein", "off", "aus", "")
        for value in truthy:
            with self.subTest(value=value):
                self.assertIs(settings.to_bool(value), True)
        for value in falsy:
            with self.subTest(value=value):
                self.assertIs(settings.to_bool(value, True), False)
        self.assertIs(settings.to_bool(None, True), True)
        self.assertIs(settings.to_bool("unknown", True), True)
        self.assertIs(settings.to_bool("unknown", False), False)

    def test_integer_normalization_and_minimums(self):
        self.assertEqual(settings.to_int("12", 8), 12)
        self.assertEqual(settings.to_int("invalid", 8), 8)
        normalized = self.repository.normalize(
            {
                **self.defaults,
                "fragments": "0",
                "playlist_end": "25",
                "youtube_chunk_size_mb": None,
            }
        )
        self.assertEqual(normalized["fragments"], 1)
        self.assertEqual(normalized["playlist_end"], 25)
        self.assertEqual(normalized["youtube_chunk_size_mb"], 64)

    def test_string_and_batch_mode_normalization(self):
        normalized = self.repository.normalize(
            {
                **self.defaults,
                "twitch_rate_limit": " 5m ",
                "batch_postprocess_mode": " after_all ",
            }
        )
        self.assertEqual(normalized["twitch_rate_limit"], "5m")
        self.assertEqual(normalized["batch_postprocess_mode"], "after_all")
        self.assertEqual(settings.clean_batch_postprocess_mode("invalid"), "after_each")

    def test_new_save_input_is_allowlisted_and_round_trips_as_utf8(self):
        description = "Café archive — 日本語"
        saved = self.repository.save(
            {
                "fragments": "17",
                "youtube_description": description,
                "unexpected_setting": "must not be added",
            }
        )
        loaded = self.repository.load()
        persisted = json.loads(self.settings_file.read_text(encoding="utf-8"))

        for payload in (saved, loaded, persisted):
            self.assertEqual(payload["fragments"], 17)
            self.assertEqual(payload["youtube_description"], description)
            self.assertNotIn("unexpected_setting", payload)
        raw = self.settings_file.read_text(encoding="utf-8")
        self.assertIn(description, raw)
        self.assertNotIn("\\u65e5", raw)
        self.assertEqual(len(self.ensure_calls), 1)

    def test_explicit_persisted_firefox_cookie_browser_remains_honored(self):
        saved = self.repository.save({"cookie_browser": "firefox"})
        loaded = self.repository.load()

        self.assertEqual(saved["cookie_browser"], "firefox")
        self.assertEqual(loaded["cookie_browser"], "firefox")

    def test_empty_path_updates_do_not_replace_working_saved_paths(self):
        active = self.media_root / "active"
        client_secret = self.runtime_dir / "custom-client-secret.json"
        self.repository.save(
            {
                "download_path": str(active),
                "youtube_client_secret_file": str(client_secret),
            }
        )

        saved = self.repository.save(
            {
                "download_path": " ",
                "youtube_client_secret_file": "",
            }
        )

        self.assertEqual(Path(saved["download_path"]), active.resolve())
        self.assertEqual(
            Path(saved["youtube_client_secret_file"]), client_secret
        )

    def test_media_paths_are_contained_and_unsafe_values_fall_back(self):
        inside = self.media_root / "active"
        outside = self.base / "outside"
        normalized = self.repository.normalize(
            {
                **self.defaults,
                "download_path": str(inside),
                "uploaded_vods_folder": str(outside / "uploaded"),
                "streamer_file": str(outside / "streamer.txt"),
                "archive_file": str(outside / "archive.txt"),
            }
        )
        self.assertEqual(Path(normalized["download_path"]), inside.resolve())
        self.assertEqual(
            Path(normalized["uploaded_vods_folder"]),
            (self.media_root / "_hochgeladen").resolve(),
        )
        self.assertEqual(
            Path(normalized["streamer_file"]), self.runtime_dir / "streamer.txt"
        )
        self.assertEqual(
            Path(normalized["archive_file"]), self.runtime_dir / "archive.txt"
        )

        escaped = self.repository.normalize(
            {**self.defaults, "download_path": str(outside)}
        )
        self.assertEqual(
            Path(escaped["download_path"]), self.media_root.resolve()
        )

    def test_filename_template_repair_contract(self):
        valid_output = "%(uploader)s/%(id)s.%(ext)s"
        valid_manual = "{date_de} - {title}"
        valid = settings.fix_template_confusion(
            {
                "output_template": valid_output,
                "manual_upload_filename_template": valid_manual,
            }
        )
        self.assertEqual(valid["output_template"], valid_output)
        self.assertEqual(valid["manual_upload_filename_template"], valid_manual)

        for unsafe_output in ("", "{date_de}.mp4", "../%(id)s.%(ext)s", "C:\\%(id)s.%(ext)s", "/%(id)s.%(ext)s"):
            with self.subTest(output_template=unsafe_output):
                repaired = settings.fix_template_confusion(
                    {
                        "output_template": unsafe_output,
                        "manual_upload_filename_template": "%(title)s",
                    }
                )
                self.assertEqual(
                    repaired["output_template"],
                    settings.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
                )
                self.assertEqual(
                    repaired["manual_upload_filename_template"],
                    settings.MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE,
                )

    def test_stale_packaged_path_repair_contract(self):
        stale = {
            "download_path": "/mnt/data/vods",
            "streamer_file": "/mnt/data/vods/streamer.txt",
            "archive_file": "/home/oai/archive.txt",
            "youtube_client_secret_file": "\\mnt\\data\\client_secret.json",
            "youtube_token_file": "/mnt/data/youtube-token.json",
        }

        repaired = settings.clean_stale_packaged_paths(
            stale, self.runtime_dir
        )

        self.assertEqual(repaired["download_path"], str(self.runtime_dir))
        self.assertEqual(
            repaired["streamer_file"], str(self.runtime_dir / "streamer.txt")
        )
        self.assertEqual(
            repaired["archive_file"], str(self.runtime_dir / "archive.txt")
        )
        self.assertEqual(
            repaired["youtube_client_secret_file"],
            str(self.runtime_dir / "client_secret.json"),
        )
        self.assertEqual(
            repaired["youtube_token_file"],
            str(self.runtime_dir / "youtube-token.json"),
        )

    def test_json_reader_supports_utf8_bom_and_logs_malformed_content(self):
        valid = self.runtime_dir / "valid.json"
        valid.write_text('\ufeff{"title": "Café"}', encoding="utf-8")
        self.assertEqual(settings.read_json_file(valid), {"title": "Café"})

        malformed = self.runtime_dir / "malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        messages = []
        self.assertEqual(
            settings.read_json_file(malformed, log=messages.append), {}
        )
        self.assertEqual(len(messages), 1)
        self.assertIn(f"Could not read {malformed}", messages[0])

    def test_cookie_file_environment_override_is_applied_at_normalization_time(self):
        self.environ["VOD_DASHBOARD_TWITCH_COOKIE_FILE"] = " /runtime/cookies.txt "
        normalized = self.repository.normalize(
            {**self.defaults, "cookie_file": "persisted-cookies.txt"}
        )
        self.assertEqual(normalized["cookie_file"], "/runtime/cookies.txt")

        self.environ.pop("VOD_DASHBOARD_TWITCH_COOKIE_FILE")
        normalized = self.repository.normalize(
            {**self.defaults, "cookie_file": "persisted-cookies.txt"}
        )
        self.assertEqual(normalized["cookie_file"], "persisted-cookies.txt")

    def test_explicit_legacy_migration_preserves_unknown_keys_across_save(self):
        legacy = self.base / "legacy" / "settings.json"
        legacy.parent.mkdir()
        legacy.write_text(
            json.dumps(
                {
                    "fragments": "19",
                    "include_unknown_dates": "nein",
                    "youtube_title_template": "Legacy {date_de} {title}",
                    "legacy_extension_value": "preserved",
                    "uploaded_vods_folder": "_hochgeladen",
                }
            ),
            encoding="utf-8",
        )
        self.environ["VOD_DASHBOARD_LEGACY_SETTINGS_PATH"] = str(legacy)

        loaded = self.repository.load()
        self.assertEqual(loaded["fragments"], 19)
        self.assertIs(loaded["include_unknown_dates"], False)
        self.assertEqual(loaded["legacy_extension_value"], "preserved")
        self.assertEqual(
            Path(loaded["uploaded_vods_folder"]),
            (self.media_root / "_hochgeladen").resolve(),
        )
        self.assertFalse(self.settings_file.exists())

        saved = self.repository.save({"fragments": 20, "new_unknown": "rejected"})
        persisted = json.loads(self.settings_file.read_text(encoding="utf-8"))
        for payload in (saved, persisted):
            self.assertEqual(payload["legacy_extension_value"], "preserved")
            self.assertNotIn("new_unknown", payload)

        legacy.write_text(json.dumps({"fragments": 999}), encoding="utf-8")
        reloaded = self.repository.load()
        self.assertEqual(reloaded["fragments"], 20)
        self.assertEqual(reloaded["legacy_extension_value"], "preserved")

    def test_missing_settings_never_scans_ancestor_directories(self):
        ancestor = self.runtime_dir.parent / "settings.json"
        ancestor.write_text(
            json.dumps({"fragments": 999, "ancestor_value": True}),
            encoding="utf-8",
        )

        loaded = self.repository.load()

        self.assertEqual(loaded["fragments"], 8)
        self.assertNotIn("ancestor_value", loaded)
        self.assertEqual(self.repository.legacy_settings_candidates(), [])
        self.assertFalse(self.settings_file.exists())

    def test_module_boundary_and_import_are_side_effect_free(self):
        module_path = Path(settings.__file__).resolve()
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            node.module.split(".", 1)[0]
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_modules.update(
            alias.name.split(".", 1)[0]
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("app", imported_modules)
        self.assertNotIn("flask", imported_modules)
        self.assertNotIn("subprocess", imported_modules)

        absent_media = self.base / "import-only-media"
        absent_data = self.base / "import-only-data"
        child_env = os.environ.copy()
        child_env.update(
            {
                "VOD_DASHBOARD_MEDIA_ROOT": str(absent_media),
                "VOD_DASHBOARD_DIR": str(absent_data),
                "VOD_DASHBOARD_SETTINGS": str(absent_data / "settings.json"),
            }
        )
        subprocess.run(
            [sys.executable, "-c", "import vod_dashboard.settings"],
            cwd=Path(__file__).resolve().parent.parent,
            env=child_env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertFalse(absent_media.exists())
        self.assertFalse(absent_data.exists())


class RuntimeDataRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.app_dir = self.base / "project" / "current"
        self.media_root = self.base / "media"
        self.runtime_dir = self.base / "data"
        self.download_dir = self.media_root / "downloads"
        self.uploaded_dir = self.media_root / "_hochgeladen"
        self.streamer_path = self.runtime_dir / "streamer.txt"
        self.archive_path = self.runtime_dir / "archive.txt"
        self.values = {
            **settings.DEFAULT_SETTINGS,
            "download_path": str(self.download_dir),
            "streamer_file": str(self.streamer_path),
            "archive_file": str(self.archive_path),
            "uploaded_vods_folder": str(self.uploaded_dir),
            "move_uploaded_vods": True,
        }
        self.load_count = 0

        def load_settings():
            self.load_count += 1
            return self.values

        self.repository = settings.RuntimeDataRepository(
            app_dir=self.app_dir,
            default_dashboard_dir=self.runtime_dir,
            media_policy=MediaPathPolicy(self.media_root),
            fixed_streamer_file=self.streamer_path,
            fixed_archive_file=self.archive_path,
            fixed_uploaded_vods_folder=self.uploaded_dir,
            settings_loader=load_settings,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_streamer_cleanup_duplicate_order_and_allowed_characters(self):
        maximum = "A" * 25
        too_long = "B" * 26
        cleaned = settings.clean_streamer_names(
            [
                " @FirstStreamer ",
                "firststreamer",
                "Second_2",
                "# comment",
                "",
                None,
                maximum,
                too_long,
                "not-allowed",
                "Café",
                "日本語",
            ]
        )

        self.assertEqual(cleaned, ["FirstStreamer", "Second_2", maximum])

    def test_streamer_reader_repairs_newlines_delimiters_and_encoding(self):
        self.streamer_path.parent.mkdir(parents=True)
        self.streamer_path.write_text(
            "\ufeffAlpha\\nBravo\\\\nCharlie,Delta;Echo\nALPHA\n",
            encoding="utf-8",
        )
        self.assertEqual(
            settings.read_streamers_from_path(self.streamer_path),
            ["Alpha", "Bravo", "Charlie", "Delta", "Echo"],
        )

        self.streamer_path.write_bytes(b"caf\xe9\nLatin_Name\n")
        self.assertEqual(
            settings.read_streamers_from_path(self.streamer_path),
            ["Latin_Name"],
        )

    def test_streamer_read_write_round_trip_and_empty_missing_behavior(self):
        missing = self.runtime_dir / "missing.txt"
        self.assertEqual(settings.read_streamers_from_path(missing), [])

        written = settings.write_streamers_to_path(
            self.streamer_path,
            ["@Alpha", "beta", "ALPHA", "invalid-name"],
        )
        self.assertEqual(written, ["Alpha", "beta"])
        self.assertEqual(
            self.streamer_path.read_text(encoding="utf-8"), "Alpha\nbeta\n"
        )
        self.assertEqual(
            settings.read_streamers_from_path(self.streamer_path),
            ["Alpha", "beta"],
        )

        self.assertEqual(
            settings.write_streamers_to_path(self.streamer_path, []), []
        )
        self.assertEqual(self.streamer_path.read_text(encoding="utf-8"), "")

    def test_archive_parsing_duplicates_malformed_and_empty_lines(self):
        self.archive_path.parent.mkdir(parents=True)
        self.archive_path.write_text(
            "\ufefftwitch 1234567890\n"
            "twitch 1234567890\n"
            "garbage-line\n"
            "youtube abc987654 suffix\n"
            "short 12345\n"
            "\n",
            encoding="utf-8",
        )

        ids = settings.archive_ids_from_path(self.archive_path)

        self.assertEqual(
            ids,
            {
                "twitch 1234567890",
                "twitch",
                "1234567890",
                "garbage-line",
                "youtube abc987654 suffix",
                "youtube",
                "abc987654",
                "987654",
                "suffix",
                "short 12345",
                "short",
                "12345",
            },
        )
        self.assertEqual(
            self.repository.archive_ids(self.values), ids
        )
        self.assertEqual(
            settings.archive_ids_from_path(self.base / "missing-archive.txt"),
            set(),
        )

    def test_ensure_files_fresh_runtime_exact_layout_and_idempotency(self):
        self.repository.ensure_files()

        created = {
            path.relative_to(self.base)
            for path in self.base.rglob("*")
        }
        self.assertEqual(
            created,
            {
                Path("data"),
                Path("data/streamer.txt"),
                Path("data/archive.txt"),
                Path("media"),
                Path("media/downloads"),
                Path("media/_hochgeladen"),
            },
        )
        self.assertEqual(self.load_count, 1)
        self.assertFalse((self.runtime_dir / "dashboard-settings.json").exists())

        self.streamer_path.write_text("Alpha\n", encoding="utf-8")
        self.archive_path.write_text("twitch 1234567890\n", encoding="utf-8")
        self.repository.ensure_files(self.values)
        self.assertEqual(
            self.streamer_path.read_text(encoding="utf-8"), "Alpha\n"
        )
        self.assertEqual(
            self.archive_path.read_text(encoding="utf-8"),
            "twitch 1234567890\n",
        )
        self.assertEqual(
            {path.relative_to(self.base) for path in self.base.rglob("*")},
            created,
        )

    def test_ensure_files_does_not_create_uploaded_directory_when_disabled(self):
        values = {**self.values, "move_uploaded_vods": False}
        self.repository.ensure_files(values)

        self.assertTrue(self.runtime_dir.exists())
        self.assertTrue(self.download_dir.exists())
        self.assertTrue(self.streamer_path.exists())
        self.assertTrue(self.archive_path.exists())
        self.assertFalse(self.uploaded_dir.exists())

    def test_fixed_file_locations_ignore_persisted_path_values(self):
        outside = self.base / "outside"
        values = {
            **self.values,
            "streamer_file": str(outside / "streamer.txt"),
            "archive_file": str(outside / "archive.txt"),
        }

        self.assertEqual(
            self.repository.streamer_file(values), self.streamer_path
        )
        self.assertEqual(self.repository.archive_file(values), self.archive_path)
        self.repository.write_streamers(["Alpha"], values)
        self.assertEqual(self.repository.read_streamers(values), ["Alpha"])
        self.assertFalse(outside.exists())

    def test_legacy_streamer_candidates_are_diagnostic_only_and_sorted(self):
        older = self.app_dir.parent / "old" / "streamer.txt"
        newer = self.app_dir / "legacy" / "streamer.txt"
        older.parent.mkdir(parents=True)
        newer.parent.mkdir(parents=True)
        older.write_text("OlderName\n", encoding="utf-8")
        newer.write_text("NewerName\n", encoding="utf-8")
        now = time.time()
        os.utime(older, (now - 60, now - 60))
        os.utime(newer, (now, now))

        candidates = self.repository.legacy_streamer_candidates()

        self.assertEqual(candidates, [newer, older])
        self.assertEqual(self.repository.read_streamers(self.values), [])
        self.assertEqual(
            self.streamer_path.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            self.repository.legacy_streamer_candidates(), [newer, older]
        )


if __name__ == "__main__":
    unittest.main()
