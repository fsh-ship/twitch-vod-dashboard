import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vod_dashboard import dashboard_state


class DiagnosticFilesystemTests(unittest.TestCase):
    def test_missing_directory_writability_check_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing" / "nested"

            self.assertTrue(dashboard_state.directory_is_writable(target))
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    def test_existing_file_is_not_reported_as_writable_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "file.txt"
            target.write_text("data", encoding="utf-8")

            self.assertFalse(dashboard_state.directory_is_writable(target))

    def test_access_failure_is_reported_without_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "vod_dashboard.dashboard_state.os.access", return_value=False
        ) as access:
            target = Path(temp_dir)

            self.assertFalse(dashboard_state.directory_is_writable(target))
            access.assert_called_once_with(target, dashboard_state.os.W_OK)


class DashboardStatusPayloadTests(unittest.TestCase):
    def test_empty_jobs_preserve_zero_counts_and_supplied_status_objects(self):
        youtube = {"connected": False}
        disk = {"ok": True}

        payload = dashboard_state.dashboard_status_payload(
            [], youtube, disk, "Resumable", 64
        )

        self.assertEqual(
            payload,
            {
                "jobs_total": 0,
                "jobs_active": 0,
                "jobs_failed": 0,
                "jobs_finished": 0,
                "youtube": youtube,
                "disk": disk,
                "upload_mode": "Resumable",
                "upload_chunk_mb": 64,
            },
        )
        self.assertIs(payload["youtube"], youtube)
        self.assertIs(payload["disk"], disk)

    def test_existing_job_statuses_are_counted_without_reordering_or_mutation(self):
        jobs = [
            {"id": "1", "status": "wartet"},
            {"id": "2", "status": "läuft"},
            {"id": "3", "status": "fertig"},
            {"id": "4", "status": "fehler"},
            {"id": "5", "status": "unknown"},
            {"id": "6"},
        ]

        payload = dashboard_state.dashboard_status_payload(
            jobs, {}, {}, "Simple", 8
        )

        self.assertEqual(payload["jobs_total"], 6)
        self.assertEqual(payload["jobs_active"], 2)
        self.assertEqual(payload["jobs_failed"], 1)
        self.assertEqual(payload["jobs_finished"], 1)
        self.assertEqual([job["id"] for job in jobs], ["1", "2", "3", "4", "5", "6"])

    def test_malformed_non_mapping_job_still_raises(self):
        with self.assertRaises(AttributeError):
            dashboard_state.dashboard_status_payload(
                [None], {}, {}, "Simple", 8
            )


class RuntimeStatePayloadTests(unittest.TestCase):
    def test_application_state_preserves_values_order_and_duplicate_streamers(self):
        settings = {"playlist_end": 25}
        streamers = ["Beta", "Alpha", "Beta"]

        payload = dashboard_state.application_state_payload(
            settings,
            "/runtime/settings.json",
            "/app/dashboard-settings.json",
            False,
            streamers,
            3,
            download_path_exists=True,
            streamer_file_exists=True,
            streamer_file_resolved="/runtime/streamer.txt",
            streamer_file_forced=Path("/runtime/streamer.txt"),
            archive_file_exists=False,
            archive_file_resolved="/runtime/archive.txt",
            archive_file_forced=Path("/runtime/archive.txt"),
        )

        self.assertIs(payload["settings"], settings)
        self.assertIs(payload["streamers"], streamers)
        self.assertEqual(payload["archive_count"], 3)
        self.assertFalse(payload["persistent_settings_exists"])
        self.assertTrue(payload["download_path_exists"])
        self.assertEqual(payload["streamers"], ["Beta", "Alpha", "Beta"])
        self.assertEqual(
            payload["archive_file_forced"], str(Path("/runtime/archive.txt"))
        )

    def test_settings_status_limits_legacy_candidates_to_first_ten_in_order(self):
        candidates = [Path(f"legacy-{index}.json") for index in range(12)]

        payload = dashboard_state.settings_status_payload(
            "runtime/settings.json",
            False,
            True,
            "app/dashboard-settings.json",
            candidates,
            "media/downloads",
            "runtime/streamer.txt",
            "runtime/archive.txt",
        )

        self.assertEqual(
            payload["legacy_candidates"],
            [str(path) for path in candidates[:10]],
        )
        self.assertFalse(payload["settings_exists"])
        self.assertTrue(payload["settings_parent_exists"])
        self.assertEqual(payload["download_path"], "media/downloads")

    def test_streamer_status_preserves_order_duplicates_and_diagnostic_values(self):
        streamers = ["Beta", "Alpha", "Beta"]
        candidates = [Path(f"streamer-{index}.txt") for index in range(11)]

        payload = dashboard_state.streamer_status_payload(
            "runtime/streamer.txt",
            exists=True,
            parent_exists=True,
            streamers=streamers,
            legacy_candidates=candidates,
            raw_preview="Alpha\\nBeta",
            has_literal_newlines=True,
        )

        self.assertIs(payload["streamers"], streamers)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            payload["legacy_candidates"],
            [str(path) for path in candidates[:10]],
        )
        self.assertEqual(payload["raw_preview"], "Alpha\\nBeta")
        self.assertTrue(payload["has_literal_newlines"])
        self.assertEqual(
            payload["note"],
            "Legacy dashboard streamer.txt files are shown for reference but are not read automatically.",
        )

    def test_missing_streamer_file_values_remain_explicit(self):
        payload = dashboard_state.streamer_status_payload(
            "runtime/streamer.txt",
            exists=False,
            parent_exists=False,
            streamers=[],
            legacy_candidates=[],
            raw_preview="",
            has_literal_newlines=False,
        )

        self.assertFalse(payload["exists"])
        self.assertFalse(payload["parent_exists"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["streamers"], [])
        self.assertEqual(payload["raw_preview"], "")


if __name__ == "__main__":
    unittest.main()
