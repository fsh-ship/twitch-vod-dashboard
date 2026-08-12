import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_IMPORT_TMP = tempfile.TemporaryDirectory()
_IMPORT_BASE = Path(_IMPORT_TMP.name)
_OLD_ENV = {
    name: os.environ.get(name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
    )
}
os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(_IMPORT_BASE / "media")
os.environ["VOD_DASHBOARD_DIR"] = str(_IMPORT_BASE / "data")
os.environ["VOD_DASHBOARD_SETTINGS"] = str(_IMPORT_BASE / "data" / "settings.json")
os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"

import app as dashboard  # noqa: E402


def tearDownModule():
    _IMPORT_TMP.cleanup()
    for name, value in _OLD_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class FilesystemContainmentTests(unittest.TestCase):
    def setUp(self):
        self.media_root = dashboard.MEDIA_ROOT
        self.data_root = dashboard.DEFAULT_DASHBOARD_DIR
        shutil.rmtree(self.media_root, ignore_errors=True)
        shutil.rmtree(self.data_root, ignore_errors=True)
        self.media_root.mkdir(parents=True)
        self.data_root.mkdir(parents=True)
        dashboard.LOG_FILE = self.data_root / "dashboard.log"
        dashboard.jobs.clear()
        dashboard.job_counter = 0
        dashboard.app.config["VOD_AUTH_DISABLED"] = True
        self.client = dashboard.app.test_client()
        status = self.client.get("/api/auth/status")
        self.security_headers = {"X-CSRF-Token": status.get_json()["csrf_token"]}

        self.valid_file = self.media_root / "channel" / "valid.mp4"
        self.valid_file.parent.mkdir(parents=True)
        self.valid_file.write_bytes(b"valid video placeholder")

        self.outside_dir = _IMPORT_BASE / "outside"
        shutil.rmtree(self.outside_dir, ignore_errors=True)
        self.outside_dir.mkdir(parents=True)
        self.outside_file = self.outside_dir / "outside.mp4"
        self.outside_file.write_bytes(b"outside video placeholder")

    def settings(self):
        return {
            **dashboard.DEFAULT_SETTINGS,
            "download_path": str(self.media_root),
            "uploaded_vods_folder": str(self.media_root / "_hochgeladen"),
        }

    def assert_rejected_response(self, response):
        self.assertGreaterEqual(response.status_code, 400)
        body = response.get_json() or {}
        self.assertIn("outside", str(body.get("error", "")))

    def create_escape_symlink(self):
        link = self.media_root / "escape.mp4"
        try:
            link.symlink_to(self.outside_file)
        except (NotImplementedError, OSError) as exc:
            if os.name == "nt":
                self.skipTest(f"Symlinks are not supported in this environment: {exc}")
            raise
        return link

    def test_valid_file_inside_media_root_is_accepted(self):
        resolved = dashboard.safe_local_video_path(self.valid_file, self.settings())
        self.assertEqual(resolved, self.valid_file.resolve())

    def test_parent_traversal_is_rejected(self):
        traversal = self.media_root / ".." / "outside" / self.outside_file.name
        with self.assertRaisesRegex(RuntimeError, "outside"):
            dashboard.safe_local_video_path(traversal, self.settings())

    def test_absolute_outside_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "outside"):
            dashboard.safe_local_video_path(self.outside_file, self.settings())

    def test_symlink_escape_is_rejected(self):
        link = self.create_escape_symlink()
        with self.assertRaisesRegex(RuntimeError, "outside"):
            dashboard.safe_local_video_path(link, self.settings())

    def test_settings_api_normalizes_download_and_uploaded_paths_to_media_root(self):
        response = self.client.post(
            "/api/settings",
            json={
                "download_path": str(self.outside_dir),
                "uploaded_vods_folder": str(self.outside_dir / "uploaded"),
                "VOD_DASHBOARD_MEDIA_ROOT": str(self.outside_dir),
            },
            headers=self.security_headers,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(Path(body["download_path"]), self.media_root)
        self.assertEqual(Path(body["uploaded_vods_folder"]), self.media_root / "_hochgeladen")
        self.assertEqual(dashboard.MEDIA_ROOT, self.media_root)
        self.assertNotIn("VOD_DASHBOARD_MEDIA_ROOT", body)

    def test_relative_download_path_is_resolved_below_media_root(self):
        settings = dashboard.normalize_settings(
            {**dashboard.DEFAULT_SETTINGS, "download_path": "active-downloads"}
        )
        self.assertEqual(
            Path(settings["download_path"]),
            (self.media_root / "active-downloads").resolve(),
        )

    def test_download_output_template_cannot_escape_media_root(self):
        settings = dashboard.normalize_settings(
            {**dashboard.DEFAULT_SETTINGS, "output_template": "../../outside/%(id)s.%(ext)s"}
        )
        self.assertEqual(settings["output_template"], dashboard.YTDLP_DEFAULT_OUTPUT_TEMPLATE)
        command, list_path = dashboard.build_download_command(
            ["https://www.twitch.tv/videos/123456"], settings
        )
        try:
            self.assertEqual(Path(command[command.index("-P") + 1]), self.media_root)
            self.assertEqual(
                command[command.index("-o") + 1],
                dashboard.YTDLP_DEFAULT_OUTPUT_TEMPLATE,
            )
        finally:
            list_path.unlink(missing_ok=True)

    def test_listing_includes_valid_file(self):
        response = self.client.get("/api/local-videos?include_uploaded=1")
        self.assertEqual(response.status_code, 200)
        paths = {item["path"] for item in response.get_json()["videos"]}
        self.assertIn(str(self.valid_file.resolve()), paths)
        self.assertNotIn(str(self.outside_file.resolve()), paths)

    def test_listing_excludes_symlink_escape(self):
        link = self.create_escape_symlink()
        response = self.client.get("/api/local-videos?include_uploaded=1")
        self.assertEqual(response.status_code, 200)
        paths = {item["path"] for item in response.get_json()["videos"]}
        self.assertNotIn(str(link), paths)
        self.assertNotIn(str(self.outside_file.resolve()), paths)

    def test_existing_file_endpoints_reject_outside_paths(self):
        endpoint_payloads = {
            "/api/local-video/open": {"path": str(self.outside_file), "mode": "select"},
            "/api/local-video/mark-uploaded": {"path": str(self.outside_file)},
            "/api/local-video/move-uploaded": {"path": str(self.outside_file), "force": True},
            "/api/local-video/delete": {
                "path": str(self.outside_file),
                "confirm_name": self.outside_file.name,
            },
            "/api/youtube/upload-local": {"paths": [str(self.outside_file)]},
            "/api/youtube/upload-file": {"path": str(self.outside_file)},
            "/api/youtube/preview-file": {"path": str(self.outside_file)},
        }
        for endpoint, payload in endpoint_payloads.items():
            with self.subTest(endpoint=endpoint):
                self.assert_rejected_response(
                    self.client.post(endpoint, json=payload, headers=self.security_headers)
                )

    def test_manual_prepare_endpoint_reports_outside_path_without_touching_it(self):
        before = self.outside_file.read_bytes()
        response = self.client.post(
            "/api/manual-upload/prepare-local",
            json={"paths": [str(self.outside_file)]},
            headers=self.security_headers,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["prepared"], [])
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("outside", body["errors"][0]["error"])
        self.assertEqual(self.outside_file.read_bytes(), before)

    def test_file_operation_helpers_reject_outside_path_before_side_effects(self):
        settings = self.settings()
        helpers = (
            lambda: dashboard.prepare_file_for_manual_youtube_upload(self.outside_file, settings),
            lambda: dashboard.upload_video_to_youtube(self.outside_file, settings),
            lambda: dashboard.move_video_bundle_verified(self.outside_file, settings),
            lambda: dashboard.delete_video_bundle_permanently(self.outside_file, settings),
            lambda: dashboard.create_upload_job([str(self.outside_file)]),
        )
        for helper in helpers:
            with self.subTest(helper=helper):
                with self.assertRaisesRegex(RuntimeError, "outside"):
                    helper()

    def test_background_upload_worker_revalidates_queued_paths(self):
        dashboard.jobs["worker-test"] = {
            "id": "worker-test",
            "status": "wartet",
            "urls": [str(self.outside_file)],
            "log": [],
            "returncode": None,
        }
        with mock.patch.object(dashboard, "get_youtube_service"), mock.patch.object(
            dashboard, "upload_video_to_youtube"
        ) as upload:
            dashboard.run_upload_job("worker-test")
        upload.assert_not_called()
        self.assertEqual(dashboard.jobs["worker-test"]["status"], "fehler")
        self.assertEqual(dashboard.jobs["worker-test"]["returncode"], 1)

    def test_snapshot_helpers_exclude_symlink_escape(self):
        link = self.create_escape_symlink()
        snapshot = dashboard.snapshot_video_files(self.settings())
        self.assertNotIn(str(link), snapshot)
        self.assertNotIn(str(self.outside_file.resolve()), snapshot)

    def test_snapshot_helpers_include_valid_file(self):
        snapshot = dashboard.snapshot_video_files(self.settings())
        self.assertIn(str(self.valid_file.resolve()), snapshot)

    def test_open_folder_uses_normalized_media_root_when_setting_is_outside(self):
        dashboard.SETTINGS_FILE.write_text(
            '{"download_path": "' + str(self.outside_dir).replace("\\", "\\\\") + '"}',
            encoding="utf-8",
        )
        with mock.patch.object(dashboard.os, "startfile", create=True) as startfile:
            response = self.client.post(
                "/api/open-folder", json={}, headers=self.security_headers
            )
        self.assertEqual(response.status_code, 200)
        startfile.assert_called_once_with(str(self.media_root))


if __name__ == "__main__":
    unittest.main()
