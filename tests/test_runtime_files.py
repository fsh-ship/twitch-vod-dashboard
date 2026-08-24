import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vod_dashboard import runtime, runtime_files, settings, youtube


class AtomicRuntimeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_successful_utf8_write_replaces_content_and_preserves_private_mode(self):
        path = self.root / "token.json"
        path.write_text("old", encoding="utf-8")
        runtime_files.atomic_write_text(path, "Hallüüü", mode=0o600)
        self.assertEqual(path.read_text(encoding="utf-8"), "Hallüüü")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_temp_fsync_preserves_primary_and_cleans_temporary_file(self):
        path = self.root / "dashboard-settings.json"
        path.write_bytes(b'{"old": true}\n')
        with mock.patch("vod_dashboard.runtime_files.os.fsync", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                runtime_files.atomic_write_text(path, '{"new": true}\n')
        self.assertEqual(path.read_bytes(), b'{"old": true}\n')
        self.assertEqual(list(self.root.glob(".dashboard-settings.json.*.tmp")), [])

    def test_streamer_write_is_atomic_when_temp_write_fails(self):
        path = self.root / "streamer.txt"
        path.write_text("oldstreamer\n", encoding="utf-8")
        with mock.patch("vod_dashboard.settings.atomic_write_text", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                settings.write_streamers_to_path(path, ["newstreamer"])
        self.assertEqual(path.read_text(encoding="utf-8"), "oldstreamer\n")

    def test_token_write_failure_preserves_old_token_without_secret_log(self):
        path = self.root / "youtube-token.json"
        path.write_text('{"token":"old"}', encoding="utf-8")
        with mock.patch("vod_dashboard.youtube.atomic_write_text", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                youtube._persist_youtube_token(path, '{"token":"new-secret"}')
        self.assertEqual(path.read_text(encoding="utf-8"), '{"token":"old"}')

    def test_file_logging_never_masks_the_calling_exception_or_rotation_failure(self):
        path = self.root / "dashboard.log"
        with mock.patch.object(Path, "mkdir", side_effect=OSError(28, "full")):
            runtime.log_line("diagnostic", path)
        path.write_text("x", encoding="utf-8")
        with mock.patch.object(Path, "replace", side_effect=OSError(28, "full")):
            runtime.log_line("diagnostic", path, max_bytes=0)

