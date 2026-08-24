import unittest
from pathlib import Path

from vod_dashboard.auto_vod_status import public_auto_vod_status


ROOT = Path(__file__).resolve().parents[1]


class AutoVodPublicStatusTests(unittest.TestCase):
    def status(self, result=None, **updates):
        snapshot = {
            "running": True,
            "thread_alive": True,
            "in_progress": False,
            "last_started_at": "2026-08-25T12:00:00Z",
            "last_finished_at": "2026-08-25T12:01:00Z",
            "next_check_at": "2026-08-25T13:01:00Z",
            "last_result": result,
        }
        snapshot.update(updates)
        return public_auto_vod_status(
            snapshot, initialized=True, enabled=True, watched_count=3, poll_minutes=60
        )

    def test_storage_bytes_are_rounded_and_no_internal_details_leak(self):
        value = self.status(
            {
                "action": "storage_insufficient",
                "storage_state": "insufficient",
                "storage_free_bytes": int(38.24 * 1024 ** 3),
                "storage_required_bytes": 50 * 1024 ** 3,
                "storage_blocked_count": 1,
                "error": "C:/srv/private/device secret",
                "path": "C:/srv/private",
                "errors": [{"vod_id": "2854443252"}],
            }
        )
        result = value["last_result"]
        self.assertEqual(result["storage_free_gb"], 38.2)
        self.assertEqual(result["storage_required_gb"], 50.0)
        self.assertEqual(result["storage_state"], "insufficient")
        self.assertEqual(result["storage_blocked_count"], 1)
        rendered = repr(value)
        self.assertNotIn("private", rendered)
        self.assertNotIn("2854443252", rendered)
        self.assertNotIn("storage_free_bytes", rendered)

    def test_unavailable_migration_and_baseline_are_safe_allowlisted_states(self):
        unavailable = self.status(
            {"action": "storage_unavailable", "storage_state": "unavailable", "error": "disk /dev/sda failed"}
        )
        migration = self.status({"action": "migration_required", "storage_state": "sufficient"})
        baseline = self.status(
            {"action": "checked", "baseline_established_count": 3, "baseline_initialized_count": 3}
        )
        self.assertEqual(unavailable["last_result"]["action"], "storage_unavailable")
        self.assertIsNone(unavailable["last_result"]["storage_free_gb"])
        self.assertEqual(migration["last_result"]["action"], "migration_required")
        self.assertEqual(baseline["last_result"]["baseline_established_count"], 3)
        self.assertEqual(baseline["last_result"]["baseline_initialized_count"], 3)

    def test_normal_contract_remains_compact_and_unknown_values_are_sanitized(self):
        value = self.status(
            {"action": "private_exception_message", "storage_state": "secret_mount", "queued_count": 2}
        )
        result = value["last_result"]
        self.assertEqual(result["action"], "checked")
        self.assertEqual(result["storage_state"], "not_checked")
        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(set(value), {
            "initialized", "enabled", "poll_minutes", "running", "thread_alive",
            "in_progress", "last_started_at", "last_finished_at", "next_check_at",
            "last_result", "watched_count",
        })


class AutoVodFrontendContractTests(unittest.TestCase):
    def test_compact_status_priority_and_safe_dom_updates_are_present(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("function autoVodStatusPresentation")
        end = source.index("function autoRecorderStatusPresentation", start)
        section = source[start:end]
        self.assertLess(section.index("migration_required"), section.index("storage_unavailable"))
        self.assertLess(section.index("storage_unavailable"), section.index("storage_insufficient"))
        for text in (
            "Auto VOD · Off",
            "Auto VOD · No streamers",
            "Auto VOD · Ready",
            "Auto VOD · Running",
            "Waiting for storage",
            "baseline migration is required",
            "could not be checked",
        ):
            self.assertIn(text, section)
        self.assertIn("title.textContent = view.title", section)
        self.assertIn("detail.textContent = view.detail", section)
        self.assertNotIn("box.innerHTML", section)

    def test_check_now_stays_asynchronous_and_remains_enabled_for_storage_recovery(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        js_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        template_source = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        route = app_source[app_source.index("def api_auto_vod_check_now"):app_source.index("@app.post(\"/api/settings\")")]
        self.assertIn("wake_auto_vod_monitor()", route)
        self.assertNotIn("discover_streamer_vods", route)
        refresh = js_source[js_source.index("async function refreshAutoVodStatus"):js_source.index("function autoRecorderStatusPresentation")]
        self.assertIn("snapshot.enabled !== true", refresh)
        self.assertNotIn("storage_insufficient", refresh)
        self.assertIn('id="checkAutoVodNow" disabled', template_source)


if __name__ == "__main__":
    unittest.main()
