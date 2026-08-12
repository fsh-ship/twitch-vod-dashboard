import ast
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vod_dashboard import media


class MediaPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.outside_root = self.base / "outside"
        self.media_root.mkdir()
        self.outside_root.mkdir()
        self.policy = media.MediaPathPolicy(self.media_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_relative_absolute_nested_and_nonexistent_paths(self):
        nested = self.media_root / "streamer" / "season"
        nested.mkdir(parents=True)
        existing = nested / "vod.mp4"
        existing.write_bytes(b"video")

        self.assertEqual(
            self.policy.resolve_media_path("streamer/season/vod.mp4"),
            existing.resolve(),
        )
        self.assertEqual(
            self.policy.resolve_media_path(existing),
            existing.resolve(),
        )
        self.assertEqual(
            self.policy.resolve_media_path("streamer/future/new.mp4"),
            (self.media_root / "streamer" / "future" / "new.mp4").resolve(),
        )
        self.assertFalse((self.media_root / "streamer" / "future").exists())

    def test_parent_traversal_and_absolute_outside_paths_are_rejected(self):
        outside_file = self.outside_root / "outside.mp4"
        outside_file.write_bytes(b"outside")

        with self.assertRaisesRegex(
            RuntimeError,
            "outside the administrator-configured media root",
        ):
            self.policy.resolve_media_path("../outside/outside.mp4")
        with self.assertRaisesRegex(
            RuntimeError,
            "outside the administrator-configured media root",
        ):
            self.policy.resolve_media_path(outside_file)

    def test_existing_video_validation_and_extension_rules(self):
        video = self.media_root / "valid.MKV"
        video.write_bytes(b"video")
        invalid = self.media_root / "notes.txt"
        invalid.write_text("not a video", encoding="utf-8")

        self.assertEqual(
            self.policy.safe_local_video_path(video, {}, must_exist=True),
            video.resolve(),
        )
        with self.assertRaisesRegex(RuntimeError, "Unsupported VOD file type"):
            self.policy.safe_local_video_path(invalid, {}, must_exist=True)
        with self.assertRaisesRegex(RuntimeError, "File not found"):
            self.policy.safe_local_video_path(
                self.media_root / "missing.mp4", {}, must_exist=True
            )
        self.assertEqual(
            self.policy.safe_local_video_path(
                self.media_root / "future.mp4", {}, must_exist=False
            ),
            (self.media_root / "future.mp4").resolve(),
        )

    def test_require_file_and_empty_path_errors_are_preserved(self):
        directory = self.media_root / "directory.mp4"
        directory.mkdir()
        with self.assertRaisesRegex(RuntimeError, "No media path provided"):
            self.policy.resolve_media_path("")
        with self.assertRaisesRegex(RuntimeError, "Path is not a file"):
            self.policy.resolve_media_path(
                directory,
                must_exist=True,
                require_file=True,
                allowed_extensions=media.VIDEO_EXTENSIONS,
            )

    def test_symlink_escape_is_rejected_where_supported(self):
        outside_file = self.outside_root / "outside.mp4"
        outside_file.write_bytes(b"outside")
        link = self.media_root / "escape.mp4"
        try:
            link.symlink_to(outside_file)
        except (NotImplementedError, OSError) as exc:
            if os.name == "nt":
                self.skipTest(f"Symlinks are not supported in this environment: {exc}")
            raise

        with self.assertRaisesRegex(
            RuntimeError,
            "outside the administrator-configured media root",
        ):
            self.policy.safe_local_video_path(link, {}, must_exist=True)

    def test_download_path_normalization_is_preserved(self):
        relative = self.policy.download_path({"download_path": "active-downloads"})
        self.assertEqual(
            relative,
            (self.media_root / "active-downloads").resolve(),
        )

        outside = self.policy.download_path(
            {"download_path": str(self.outside_root)}
        )
        self.assertEqual(outside, self.media_root.resolve())

        defaulted = self.policy.download_path({"download_path": ""})
        self.assertEqual(defaulted, self.media_root.resolve())

    def test_uploaded_vods_folder_normalization_is_preserved(self):
        fallback = self.media_root / "_hochgeladen"
        relative = self.policy.uploaded_vods_folder(
            {"uploaded_vods_folder": "uploaded"}, fallback
        )
        self.assertEqual(relative, (self.media_root / "uploaded").resolve())

        outside = self.policy.uploaded_vods_folder(
            {"uploaded_vods_folder": str(self.outside_root)}, fallback
        )
        self.assertEqual(outside, fallback.resolve())

        defaulted = self.policy.uploaded_vods_folder(
            {"uploaded_vods_folder": ""}, fallback
        )
        self.assertEqual(defaulted, fallback.resolve())

    def test_is_path_inside_uses_the_same_resolved_containment_boundary(self):
        nested = self.media_root / "nested" / "vod.mp4"
        self.assertTrue(self.policy.is_path_inside(nested, self.media_root))
        self.assertTrue(
            self.policy.is_path_inside(nested, self.media_root / "nested")
        )
        self.assertFalse(
            self.policy.is_path_inside(self.outside_root / "vod.mp4", self.media_root)
        )
        self.assertFalse(
            self.policy.is_path_inside(nested, self.outside_root)
        )

    def test_policy_construction_and_module_import_have_no_write_side_effects(self):
        absent_root = self.base / "not-created"
        policy = media.MediaPathPolicy(absent_root)
        self.assertEqual(policy.media_root, absent_root.resolve())
        self.assertFalse(absent_root.exists())

        module_path = Path(media.__file__).resolve()
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
                "json",
                "pathlib",
                "shutil",
                "typing",
            },
        )
        self.assertNotIn("import app", source)
        self.assertNotIn("flask", source.lower())
        self.assertNotIn("os.environ", source)
        self.assertNotIn("subprocess", source)


class MediaFileOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / "media"
        self.outside_root = self.base / "outside"
        self.download_root = self.media_root / "downloads"
        self.uploaded_root = self.media_root / "_hochgeladen"
        self.download_root.mkdir(parents=True)
        self.outside_root.mkdir()
        self.policy = media.MediaPathPolicy(self.media_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def settings(self, **overrides):
        values = {
            "download_path": str(self.download_root),
            "uploaded_vods_folder": str(self.uploaded_root),
            "youtube_uploaded_files": [],
        }
        values.update(overrides)
        return values

    def make_video(self, relative="Example/vod.mp4", content=b"video"):
        path = self.download_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.resolve()

    def test_upload_marker_path_write_and_read_contract(self):
        video = self.make_video()

        marker = media.local_video_marker_path(video)
        payload = self.policy.write_local_upload_marker(video, method="manual")

        self.assertEqual(marker, video.with_suffix(".uploaded.json"))
        self.assertEqual(self.policy.read_local_upload_marker(video), payload)
        self.assertEqual(payload["uploaded"], True)
        self.assertEqual(payload["method"], "manual")
        self.assertEqual(payload["video_path"], str(video))
        self.assertEqual(payload["video_name"], video.name)
        self.assertRegex(payload["uploaded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_malformed_upload_marker_returns_empty_and_logs(self):
        video = self.make_video()
        marker = media.local_video_marker_path(video)
        marker.write_text("{not-json", encoding="utf-8")
        messages = []

        self.assertEqual(
            self.policy.read_local_upload_marker(video, log=messages.append), {}
        )
        self.assertEqual(len(messages), 1)
        self.assertIn(f"Could not read upload marker {marker}", messages[0])

    def test_sidecar_discovery_accepts_only_known_in_root_files(self):
        video = self.make_video()
        expected = [
            video.with_suffix(".info.json"),
            video.with_suffix(".youtube.json"),
            video.with_suffix(".youtube-beschreibung.txt"),
            video.with_suffix(".uploaded.json"),
        ]
        for sidecar in expected:
            sidecar.write_text("{}", encoding="utf-8")
        video.with_suffix(".notes.txt").write_text("ignored", encoding="utf-8")

        self.assertEqual(self.policy.local_video_sidecars(video), expected)

        outside_video = self.outside_root / "outside.mp4"
        outside_video.write_bytes(b"outside")
        outside_video.with_suffix(".info.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.policy.local_video_sidecars(outside_video), [])

    def test_sidecar_symlink_escape_is_ignored_where_supported(self):
        video = self.make_video()
        outside_sidecar = self.outside_root / "outside.info.json"
        outside_sidecar.write_text("{}", encoding="utf-8")
        sidecar_link = video.with_suffix(".info.json")
        try:
            sidecar_link.symlink_to(outside_sidecar)
        except (NotImplementedError, OSError) as exc:
            if os.name == "nt":
                self.skipTest(f"Symlinks are not supported in this environment: {exc}")
            raise

        self.assertEqual(self.policy.local_video_sidecars(video), [])

    def test_snapshot_and_new_video_detection_contract(self):
        existing = self.make_video("Example/existing.mp4")
        ignored = existing.with_suffix(".txt")
        ignored.write_text("not video", encoding="utf-8")
        before = self.policy.snapshot_video_files(self.settings())

        added = self.make_video("Example/added.mkv")
        after = self.policy.snapshot_video_files(self.settings())
        changed = self.policy.new_video_files(before, after)

        self.assertEqual(set(before), {str(existing)})
        self.assertEqual(set(after), {str(existing), str(added)})
        self.assertEqual(changed, [added])

    def test_recently_changed_video_filtering_contract(self):
        started_at = time.time()
        recent = self.make_video("recent.mp4")
        old = self.make_video("old.mp4")
        empty = self.make_video("empty.mp4", b"")
        uploaded = self.make_video("uploaded.mp4")
        os.utime(old, (started_at - 300, started_at - 300))
        settings = self.settings(youtube_uploaded_files=[str(uploaded)])

        result = self.policy.recently_changed_video_files(
            settings, started_at, minutes_buffer=1
        )

        self.assertEqual(result, [recent])
        self.assertNotIn(old, result)
        self.assertNotIn(empty, result)
        self.assertNotIn(uploaded, result)

    def test_move_bundle_preserves_sidecars_marker_and_job_messages(self):
        video = self.make_video()
        video.with_suffix(".info.json").write_text("{}", encoding="utf-8")
        video.with_suffix(".youtube-beschreibung.txt").write_text(
            "description", encoding="utf-8"
        )
        self.policy.write_local_upload_marker(video)
        job_messages = []

        result = self.policy.move_video_bundle_verified(
            video,
            self.settings(),
            self.uploaded_root,
            job_log=job_messages.append,
        )
        moved = Path(result["new_path"])

        self.assertEqual(moved, self.uploaded_root / "Example" / video.name)
        self.assertTrue(result["ok"])
        self.assertTrue(result["source_removed"])
        self.assertFalse(video.exists())
        self.assertTrue(moved.exists())
        self.assertTrue(moved.with_suffix(".info.json").exists())
        self.assertTrue(moved.with_suffix(".youtube-beschreibung.txt").exists())
        marker = json.loads(
            moved.with_suffix(".uploaded.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["video_path"], str(moved))
        self.assertEqual(marker["video_name"], moved.name)
        self.assertEqual(
            job_messages,
            [f"VOD moved: {video} -> {moved}", "Source removed: yes"],
        )

    def test_move_bundle_uses_existing_collision_naming(self):
        video = self.make_video()
        occupied = self.uploaded_root / "Example" / video.name
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b"occupied")

        result = self.policy.move_video_bundle_verified(
            video, self.settings(), self.uploaded_root
        )

        self.assertEqual(Path(result["new_path"]).name, "vod (2).mp4")
        self.assertEqual(occupied.read_bytes(), b"occupied")
        self.assertEqual(Path(result["new_path"]).read_bytes(), b"video")

    def test_move_rejects_outside_source_and_normalizes_outside_destination(self):
        outside_video = self.outside_root / "outside.mp4"
        outside_video.write_bytes(b"outside")
        with self.assertRaisesRegex(
            RuntimeError, "outside the administrator-configured media root"
        ):
            self.policy.move_video_bundle_verified(
                outside_video, self.settings(), self.uploaded_root
            )

        video = self.make_video("safe.mp4")
        result = self.policy.move_video_bundle_verified(
            video,
            self.settings(uploaded_vods_folder=str(self.outside_root)),
            self.uploaded_root,
        )
        self.assertEqual(Path(result["new_path"]).parent, self.uploaded_root)
        self.assertTrue(outside_video.exists())

    def test_permanent_delete_removes_video_and_sidecars(self):
        video = self.make_video()
        video.with_suffix(".info.json").write_text("{}", encoding="utf-8")
        self.policy.write_local_upload_marker(video)
        expected = [video] + self.policy.local_video_sidecars(video)

        result = self.policy.delete_video_bundle_permanently(video, self.settings())

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(set(result["deleted"]), {str(path) for path in expected})
        self.assertTrue(all(not path.exists() for path in expected))

    def test_permanent_delete_rejects_outside_source(self):
        outside_video = self.outside_root / "outside.mp4"
        outside_video.write_bytes(b"outside")

        with self.assertRaisesRegex(
            RuntimeError, "outside the administrator-configured media root"
        ):
            self.policy.delete_video_bundle_permanently(
                outside_video, self.settings()
            )
        self.assertTrue(outside_video.exists())

    def test_disk_status_success_and_failure_contract(self):
        gib = 1024 ** 3
        usage = SimpleNamespace(free=5 * gib, total=10 * gib, used=5 * gib)
        with mock.patch("vod_dashboard.media.shutil.disk_usage", return_value=usage):
            status = self.policy.disk_status(self.settings())

        self.assertEqual(
            status,
            {
                "ok": True,
                "path": str(self.download_root.resolve()),
                "free_gb": 5.0,
                "total_gb": 10.0,
                "used_gb": 5.0,
            },
        )

        with mock.patch(
            "vod_dashboard.media.shutil.disk_usage", side_effect=OSError("unavailable")
        ):
            failed = self.policy.disk_status(self.settings())
        self.assertEqual(failed["ok"], False)
        self.assertEqual(failed["path"], str(self.download_root.resolve()))
        self.assertEqual(failed["error"], "unavailable")


if __name__ == "__main__":
    unittest.main()
