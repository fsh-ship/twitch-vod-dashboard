import ast
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from vod_dashboard import runtime


class RuntimePathsTests(unittest.TestCase):
    def test_windows_native_defaults_follow_userprofile_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            app_dir = base / "application"
            user_home = base / "Users" / "DashboardUser"

            paths = runtime.RuntimePaths.from_environment(
                app_dir,
                {"USERPROFILE": str(user_home)},
            )

            self.assertEqual(paths.app_dir, app_dir)
            self.assertEqual(paths.user_home, user_home)
            self.assertEqual(
                paths.default_media_root,
                user_home / "Documents" / "Twitch VODs",
            )
            self.assertEqual(paths.media_root, paths.default_media_root.resolve())
            self.assertEqual(paths.dashboard_dir, paths.media_root)
            self.assertEqual(
                paths.settings_file,
                paths.dashboard_dir / "dashboard-settings.json",
            )
            self.assertEqual(paths.local_settings_file, app_dir / "settings.json")
            self.assertEqual(paths.log_file, app_dir / "dashboard.log")
            self.assertFalse(app_dir.exists())
            self.assertFalse(user_home.exists())

    def test_linux_compatible_home_default_is_used_without_userprofile(self):
        app_dir = Path("/opt/vod-dashboard")
        mocked_home = Path("/home/dashboard")
        with mock.patch.object(runtime.Path, "home", return_value=mocked_home):
            paths = runtime.RuntimePaths.from_environment(app_dir, {})

        self.assertEqual(paths.user_home, mocked_home)
        self.assertEqual(
            paths.default_media_root,
            mocked_home / "Documents" / "Twitch VODs",
        )
        self.assertEqual(paths.media_root, paths.default_media_root.resolve())
        self.assertEqual(paths.dashboard_dir, paths.media_root)

    def test_explicit_docker_style_environment_paths_are_preserved(self):
        env = {
            "USERPROFILE": "/home/container-user",
            "VOD_DASHBOARD_MEDIA_ROOT": "/downloads",
            "VOD_DASHBOARD_DIR": "/data",
            "VOD_DASHBOARD_SETTINGS": "/data/custom-settings.json",
            "VOD_DASHBOARD_LOG_FILE": "/data/custom-dashboard.log",
        }

        paths = runtime.RuntimePaths.from_environment(Path("/app"), env)

        self.assertEqual(paths.app_dir, Path("/app"))
        self.assertEqual(paths.media_root, Path("/downloads").resolve())
        self.assertEqual(paths.dashboard_dir, Path("/data"))
        self.assertEqual(paths.settings_file, Path("/data/custom-settings.json"))
        self.assertEqual(paths.local_settings_file, Path("/app/settings.json"))
        self.assertEqual(paths.log_file, Path("/data/custom-dashboard.log"))
        self.assertEqual(paths.streamer_file, Path("/data/streamer.txt"))
        self.assertEqual(paths.archive_file, Path("/data/archive.txt"))
        self.assertEqual(
            paths.youtube_client_secret_file,
            Path("/data/client_secret.json"),
        )
        self.assertEqual(paths.youtube_token_file, Path("/data/youtube-token.json"))
        self.assertEqual(
            paths.uploaded_vods_folder,
            Path("/downloads").resolve() / "_hochgeladen",
        )

    def test_environment_is_evaluated_at_construction_time_and_paths_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            env = {
                "USERPROFILE": str(base / "home"),
                "VOD_DASHBOARD_MEDIA_ROOT": str(base / "media-one"),
                "VOD_DASHBOARD_DIR": str(base / "data-one"),
                "VOD_DASHBOARD_SETTINGS": str(base / "settings-one.json"),
                "VOD_DASHBOARD_LOG_FILE": str(base / "log-one.txt"),
            }
            first = runtime.RuntimePaths.from_environment(base / "app", env)
            env["VOD_DASHBOARD_MEDIA_ROOT"] = str(base / "media-two")
            env["VOD_DASHBOARD_DIR"] = str(base / "data-two")
            second = runtime.RuntimePaths.from_environment(base / "app", env)

            self.assertEqual(first.media_root, (base / "media-one").resolve())
            self.assertEqual(first.dashboard_dir, base / "data-one")
            self.assertEqual(second.media_root, (base / "media-two").resolve())
            self.assertEqual(second.dashboard_dir, base / "data-two")
            with self.assertRaises(FrozenInstanceError):
                first.media_root = base / "replacement"

            self.assertFalse((base / "app").exists())
            self.assertFalse((base / "media-one").exists())
            self.assertFalse((base / "media-two").exists())
            self.assertFalse((base / "data-one").exists())
            self.assertFalse((base / "data-two").exists())

    def test_bounded_utf8_logging_rotates_one_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "nested" / "dashboard.log"
            runtime.log_line("Unicode log: äöü", log_file, max_bytes=64)
            first_text = log_file.read_text(encoding="utf-8")
            self.assertRegex(
                first_text,
                r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Unicode log: äöü\n$",
            )

            log_file.write_text("x" * 64, encoding="utf-8")
            runtime.log_line("after rotation", log_file, max_bytes=64)

            backup = Path(f"{log_file}.1")
            self.assertEqual(backup.read_text(encoding="utf-8"), "x" * 64)
            self.assertIn("after rotation", log_file.read_text(encoding="utf-8"))
            self.assertFalse(Path(f"{log_file}.2").exists())

            log_file.write_text("y" * 64, encoding="utf-8")
            runtime.log_line("second rotation", log_file, max_bytes=64)
            self.assertEqual(backup.read_text(encoding="utf-8"), "y" * 64)
            self.assertFalse(Path(f"{log_file}.2").exists())

    def test_runtime_module_has_no_application_or_framework_dependencies(self):
        module_path = Path(runtime.__file__).resolve()
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
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
                "dataclasses",
                "datetime",
                "os",
                "pathlib",
                "threading",
                "typing",
            },
        )
        self.assertNotIn("import app", source)
        self.assertNotIn("flask", source.lower())
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":
    unittest.main()
