import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from vod_dashboard import local_vods
from vod_dashboard import twitch
from vod_dashboard import youtube
from vod_dashboard.media import MediaPathPolicy, local_video_marker_path


class LocalVodServiceTests(unittest.TestCase):
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

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.outside_root = self.base / "outside"
        self.app_dir = self.media_root / "application"
        self.uploaded_root = self.media_root / "_hochgeladen"
        self.media_root.mkdir()
        self.outside_root.mkdir()
        self.policy = MediaPathPolicy(self.media_root)
        self.logs = []
        self.settings = {
            "download_path": str(self.media_root),
            "uploaded_vods_folder": str(self.uploaded_root),
            "youtube_uploaded_files": [],
            "youtube_upload_history": [],
            "youtube_title_template": (
                "{streamer} VOD - {date_de} - {title}"
            ),
            "youtube_description": "Fallback description",
            "youtube_description_template": (
                "Streamer: {streamer}\nDate: {date_de}\n"
                "Original: {url}\nVOD ID: {vod_id}\n"
                "Duration: {duration}"
            ),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_video(self, relative_path, content=b"video"):
        path = self.media_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.resolve()

    @staticmethod
    def info_payload(**updates):
        payload = {
            "id": "1234567890",
            "title": "A Test Stream",
            "uploader": "Example",
            "upload_date": "20260810",
            "duration": 3661,
            "webpage_url": (
                "https://www.twitch.tv/videos/1234567890"
            ),
        }
        payload.update(updates)
        return payload

    def write_info(self, video, **updates):
        video.with_suffix(".info.json").write_text(
            json.dumps(self.info_payload(**updates), ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def live_info_payload(**updates):
        payload = {
            "id": "9876543210",
            "title": "Nika (live)",
            "description": "The actual broadcast title",
            "uploader_id": "nika_livetv",
            "uploader": "Nika",
            "channel": "Nika",
            "upload_date": "20260823",
            "duration": 90,
            "webpage_url": "https://www.twitch.tv/nika_livetv",
            "original_url": "https://www.twitch.tv/nika_livetv",
            "is_live": True,
            "live_status": "is_live",
        }
        payload.update(updates)
        return payload

    def metadata_loader(self, path, settings):
        return youtube.metadata_from_path(
            path,
            settings,
            media_policy=self.policy,
            entry_date_parser=twitch.entry_date,
            date_parser=twitch.parse_date,
            log_callback=self.logs.append,
        )

    def youtube_metadata_builder(self, path, settings):
        return youtube.build_youtube_metadata(
            path,
            settings,
            media_policy=self.policy,
            entry_date_parser=twitch.entry_date,
            date_parser=twitch.parse_date,
            metadata_loader=self.metadata_loader,
            log_callback=self.logs.append,
        )

    def marker_reader(self, path):
        return self.policy.read_local_upload_marker(
            path, log=self.logs.append
        )

    def payload(self, path, uploaded_set=None):
        return local_vods.local_video_metadata_payload(
            path,
            self.settings,
            set() if uploaded_set is None else uploaded_set,
            media_policy=self.policy,
            download_root=self.policy.download_path(self.settings),
            uploaded_root=self.policy.uploaded_vods_folder(
                self.settings, self.uploaded_root
            ),
            metadata_loader=self.metadata_loader,
            youtube_metadata_builder=self.youtube_metadata_builder,
            marker_reader=self.marker_reader,
            marker_path_builder=local_video_marker_path,
            sidecar_loader=self.policy.local_video_sidecars,
        )

    def enumerate(self, include_uploaded=False, app_dir=None):
        return local_vods.enumerate_local_vods(
            self.settings,
            include_uploaded,
            media_policy=self.policy,
            uploaded_folder_fallback=self.uploaded_root,
            app_dir=app_dir or self.app_dir,
            payload_builder=lambda path, settings, uploaded: self.payload(
                path, uploaded
            ),
            log_callback=self.logs.append,
        )

    def test_module_boundary_has_no_framework_or_application_imports(self):
        module_path = Path(local_vods.__file__).resolve()
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
            {"__future__", "datetime", "pathlib", "typing", "vod_dashboard"},
        )
        source = module_path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "import app",
            "flask",
            "jobs",
            "vod_dashboard.twitch",
            "vod_dashboard.settings",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_empty_media_root_response_contract(self):
        payload = self.enumerate()
        self.assertEqual(
            payload,
            {
                "videos": [],
                "root": str(self.media_root.resolve()),
                "uploaded_root": str(self.uploaded_root.resolve()),
                "include_uploaded": False,
                "counts": {
                    "total": 0,
                    "pending": 0,
                    "uploaded": 0,
                    "size_gb": 0.0,
                },
            },
        )

    def test_valid_vod_info_metadata_and_payload_key_contract(self):
        video = self.make_video(
            "Example/2026-08-10 - Example - Stream [1234567890].mp4"
        )
        self.write_info(video)
        payload = self.payload(video)

        self.assertEqual(set(payload), self.VIDEO_PAYLOAD_KEYS)
        self.assertEqual(payload["path"], str(video))
        self.assertEqual(payload["relative_folder"], "Example")
        self.assertEqual(payload["streamer"], "Example")
        self.assertEqual(payload["date_de"], "10.08.2026")
        self.assertEqual(payload["title"], "A Test Stream")
        self.assertEqual(payload["vod_id"], "1234567890")
        self.assertEqual(
            payload["youtube_title"],
            "Example VOD - 10.08.2026 - A Test Stream",
        )
        self.assertEqual(payload["status"], "Ready")
        self.assertIsInstance(payload["size_bytes"], int)
        self.assertIsInstance(payload["size_gb"], float)

    def test_finished_live_recording_is_ready_with_its_exact_info_sidecar(self):
        video = self.make_video(
            "nika_livetv/20260823 - Nika - LIVE - Nika (live) "
            "[9876543210].mp4"
        )
        info_path = video.with_suffix(".info.json")
        info_path.write_text(
            json.dumps(self.live_info_payload(), ensure_ascii=False),
            encoding="utf-8",
        )

        result = self.enumerate()

        self.assertEqual(len(result["videos"]), 1)
        payload = result["videos"][0]
        self.assertEqual(payload["path"], str(video))
        self.assertEqual(payload["status"], "Ready")
        self.assertEqual(payload["streamer"], "nika_livetv")
        self.assertEqual(payload["title"], "The actual broadcast title")
        self.assertEqual(payload["vod_id"], "")
        self.assertEqual(
            payload["youtube_title"],
            "nika_livetv VOD - 23.08.2026 - The actual broadcast title",
        )
        self.assertIn(
            info_path.resolve(), self.policy.local_video_sidecars(video)
        )

    def test_queue_keeps_original_title_and_displays_sanitized_youtube_title(self):
        video = self.make_video(
            "Example/2026-08-10 - Example - Stream [1234567890].mp4"
        )
        self.write_info(video, title="A < B")

        payload = self.payload(video)

        self.assertEqual(payload["title"], "A < B")
        self.assertEqual(
            payload["youtube_title"],
            "Example VOD - 10.08.2026 - A B",
        )

    def test_metadata_fallback_without_info_json(self):
        video = self.make_video(
            "Fallback/2026-08-09 Fallback title [2345678901].mkv"
        )
        payload = self.payload(video)
        self.assertEqual(payload["streamer"], "Fallback")
        self.assertEqual(payload["date_de"], "09.08.2026")
        self.assertEqual(payload["vod_id"], "2345678901")
        self.assertEqual(
            payload["title"],
            "2026-08-09 Fallback title [2345678901]",
        )

    def test_marker_state_and_malformed_marker_behavior(self):
        video = self.make_video("Example/marked.mp4")
        marker = self.policy.write_local_upload_marker(video, "manual")
        payload = self.payload(video)
        self.assertTrue(payload["manually_uploaded"])
        self.assertTrue(payload["already_uploaded"])
        self.assertEqual(payload["status"], "Manually Uploaded")
        self.assertEqual(payload["uploaded_at"], marker["uploaded_at"])

        local_video_marker_path(video).write_text(
            "{malformed", encoding="utf-8"
        )
        self.logs.clear()
        payload = self.payload(video)
        self.assertFalse(payload["manually_uploaded"])
        self.assertFalse(payload["already_uploaded"])
        self.assertEqual(payload["status"], "Ready")
        self.assertTrue(
            any("Could not read upload marker" in line for line in self.logs)
        )

    def test_youtube_sidecars_and_missing_sidecars(self):
        video = self.make_video("Example/prepared.mp4")
        missing = self.payload(video)
        self.assertFalse(missing["prepared"])
        self.assertFalse(missing["description_file_exists"])
        self.assertFalse(missing["metadata_file_exists"])

        video.with_suffix(".youtube.json").write_text(
            "{malformed but present", encoding="utf-8"
        )
        video.with_suffix(".youtube-beschreibung.txt").write_text(
            "description", encoding="utf-8"
        )
        prepared = self.payload(video)
        self.assertTrue(prepared["prepared"])
        self.assertTrue(prepared["description_file_exists"])
        self.assertTrue(prepared["metadata_file_exists"])
        self.assertEqual(prepared["status"], "Prepared for YouTube")

    def test_multiple_vods_preserve_pending_first_oldest_first_order(self):
        old_pending = self.make_video("One/old.mp4")
        new_pending = self.make_video("Two/new.mp4")
        uploaded = self.make_video("Three/uploaded.mp4")
        os.utime(old_pending, (1_700_000_000, 1_700_000_000))
        os.utime(new_pending, (1_700_100_000, 1_700_100_000))
        os.utime(uploaded, (1_699_000_000, 1_699_000_000))
        self.settings["youtube_uploaded_files"] = [str(uploaded)]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(
            [item["name"] for item in result["videos"]],
            ["old.mp4", "new.mp4", "uploaded.mp4"],
        )
        self.assertEqual(result["counts"]["pending"], 2)
        self.assertEqual(result["counts"]["uploaded"], 1)

    def test_uploaded_rows_are_hidden_until_archive_is_requested(self):
        uploaded = self.make_video("Example/uploaded.mp4")
        self.settings["youtube_uploaded_files"] = [str(uploaded)]

        self.assertEqual(self.enumerate(include_uploaded=False)["videos"], [])
        archived = self.enumerate(include_uploaded=True)
        self.assertEqual([item["name"] for item in archived["videos"]], ["uploaded.mp4"])
        self.assertEqual(archived["counts"]["pending"], 0)

    def test_unfinished_upload_is_excluded_from_ready(self):
        active = self.make_video("Example/active.mp4")

        result = local_vods.enumerate_local_vods(
            self.settings,
            False,
            media_policy=self.policy,
            uploaded_folder_fallback=self.uploaded_root,
            app_dir=self.app_dir,
            payload_builder=lambda path, settings, uploaded: self.payload(path, uploaded),
            unfinished_upload_paths={str(active)},
        )

        self.assertEqual(result["videos"], [])
        self.assertEqual(result["counts"]["pending"], 0)

    def test_known_incomplete_video_artifacts_are_not_discovered(self):
        ready = self.make_video("Example/finished.mp4")
        for name in (
            "stream.temp.mp4",
            "stream.part.mkv",
            "stream.partial.webm",
            "stream.download.mov",
            "stream.ytdl.m4v",
            "stream.mp4.temp",
        ):
            self.make_video(f"Example/{name}")

        result = self.enumerate()

        self.assertEqual([item["path"] for item in result["videos"]], [str(ready)])

    def test_live_recording_part_file_is_not_discovered(self):
        partial = self.make_video(
            "nika_livetv/20260823 - Nika - LIVE - Nika (live) "
            "[9876543210].part.mp4"
        )
        partial.with_suffix(".info.json").write_text(
            json.dumps(self.live_info_payload()), encoding="utf-8"
        )

        self.assertEqual(self.enumerate()["videos"], [])

    def test_missing_uploaded_file_is_archive_only_and_not_uploadable(self):
        missing = (self.media_root / "Example" / "removed.mp4").resolve()
        self.settings["youtube_uploaded_files"] = [str(missing)]
        self.settings["youtube_upload_history"] = [
            {
                "path": str(missing),
                "uploaded_at": "2026-08-19T12:00:00",
            }
        ]

        self.assertEqual(self.enumerate(include_uploaded=False)["videos"], [])
        archived = self.enumerate(include_uploaded=True)
        self.assertEqual(len(archived["videos"]), 1)
        payload = archived["videos"][0]
        self.assertFalse(payload["local_file_exists"])
        self.assertTrue(payload["already_uploaded"])
        self.assertEqual(payload["status"], "Local file removed")
        self.assertEqual(payload["uploaded_at"], "2026-08-19T12:00:00")
        self.assertIsNone(payload["size_bytes"])
        self.assertEqual(archived["counts"]["pending"], 0)

    def test_timestamped_uploads_sort_newest_first_before_legacy_history(self):
        older = self.make_video("Example/older-upload.mp4")
        newer = self.make_video("Example/newer-upload.mp4")
        legacy = self.make_video("Example/legacy-upload.mp4")
        self.settings["youtube_uploaded_files"] = [
            str(older),
            str(legacy),
            str(newer),
        ]
        self.settings["youtube_upload_history"] = [
            {"path": str(older), "uploaded_at": "2026-08-18T10:00:00"},
            {"path": str(newer), "uploaded_at": "2026-08-19T10:00:00"},
        ]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(
            [item["name"] for item in result["videos"]],
            ["newer-upload.mp4", "older-upload.mp4", "legacy-upload.mp4"],
        )
        self.assertEqual(result["counts"]["uploaded"], 3)

    def test_legacy_undated_history_uses_newest_append_order(self):
        oldest = (self.media_root / "Example" / "oldest.mp4").resolve()
        newest = (self.media_root / "Example" / "newest.mp4").resolve()
        self.settings["youtube_uploaded_files"] = [str(oldest), str(newest)]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(
            [item["name"] for item in result["videos"]],
            ["newest.mp4", "oldest.mp4"],
        )
        self.assertTrue(
            all(not item["uploaded_at"] for item in result["videos"])
        )

    def test_complete_history_and_truthful_count_are_not_truncated(self):
        paths = [
            (self.media_root / "History" / f"removed-{index:02d}.mp4").resolve()
            for index in range(35)
        ]
        self.settings["youtube_uploaded_files"] = [str(path) for path in paths]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(len(result["videos"]), 35)
        self.assertEqual(result["counts"]["total"], 35)
        self.assertEqual(result["counts"]["uploaded"], 35)
        self.assertEqual(len(self.settings["youtube_uploaded_files"]), 35)

    def test_missing_history_keeps_same_filename_in_distinct_streamer_folders(self):
        first = (self.media_root / "One" / "same-name.mp4").resolve()
        second = (self.media_root / "Two" / "same-name.mp4").resolve()
        self.settings["youtube_uploaded_files"] = [str(first), str(second)]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(len(result["videos"]), 2)
        self.assertEqual(
            {item["streamer"] for item in result["videos"]}, {"One", "Two"}
        )

    def test_original_and_archived_paths_for_one_upload_collapse_by_relative_identity(self):
        original = (self.media_root / "Example" / "same-vod.mp4").resolve()
        archived = (
            self.uploaded_root / "Example" / "same-vod.mp4"
        ).resolve()
        self.settings["youtube_uploaded_files"] = [str(original), str(archived)]

        result = self.enumerate(include_uploaded=True)

        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["videos"][0]["path"], str(archived))

    def test_uploaded_folder_filter_status_and_count_semantics(self):
        video = self.make_video("_hochgeladen/Example/archived.mp4")

        hidden = self.enumerate(include_uploaded=False)
        self.assertEqual(hidden["videos"], [])

        included = self.enumerate(include_uploaded=True)
        self.assertEqual(len(included["videos"]), 1)
        payload = included["videos"][0]
        self.assertEqual(payload["path"], str(video))
        self.assertTrue(payload["in_uploaded_folder"])
        self.assertTrue(payload["already_uploaded"])
        self.assertEqual(payload["status"], "Archived")
        self.assertEqual(included["counts"]["pending"], 0)
        self.assertEqual(included["counts"]["uploaded"], 1)

    def test_nested_streamer_directory_relative_folder(self):
        video = self.make_video("Streamer/2026/August/nested.mov")
        payload = self.payload(video)
        self.assertEqual(
            payload["relative_folder"],
            str(Path("Streamer") / "2026" / "August"),
        )

    def test_application_directory_and_outside_paths_are_excluded(self):
        self.make_video("application/private.mp4")
        outside = self.outside_root / "outside.mp4"
        outside.write_bytes(b"outside")

        result = self.enumerate()
        self.assertEqual(result["videos"], [])
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.payload(outside)

    def test_symlink_escape_is_excluded_where_supported(self):
        outside = self.outside_root / "outside.mp4"
        outside.write_bytes(b"outside")
        link = self.media_root / "escape.mp4"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            if os.name == "nt":
                self.skipTest(f"Symlinks are not supported: {exc}")
            raise

        result = self.enumerate(include_uploaded=True)
        self.assertEqual(result["videos"], [])
        self.assertTrue(
            any("outside" in line.lower() for line in self.logs)
        )


if __name__ == "__main__":
    unittest.main()
