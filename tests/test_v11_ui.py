from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JAVASCRIPT = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLESHEET = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _classify_download_jobs(jobs: list[dict]) -> list[dict]:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Queue state tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function parseProgress');
const end = source.indexOf('function renderQueueVodItem');
if (start < 0 || end < 0 || end <= start) throw new Error('Queue classifier source not found');
const lastResults = [];
const localVideoCache = new Map();
function rememberedSearchResults() { return []; }
eval(source.slice(start, end));
const jobs = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = queueItemsFromJobs(jobs).map(item => ({
  index: item.index,
  state: item.state,
  progress: item.progress,
  extra: item.extra,
  error: item.error,
}));
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps(jobs),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _download_job(
    item_statuses: list[str] | None,
    logs: list[str],
    *,
    status: str = "läuft",
    count: int | None = None,
) -> dict:
    item_count = count if count is not None else len(item_statuses or [None])
    job = {
        "id": "1",
        "label": "Queue regression",
        "status": status,
        "urls": [
            f"https://www.twitch.tv/videos/{1234567890 + index}"
            for index in range(item_count)
        ],
        "log": logs,
    }
    if item_statuses is not None:
        job["item_statuses"] = item_statuses
    return job


class _IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids.extend(value for key, value in attrs if key == "id" and value)


class V11UiContractTests(unittest.TestCase):
    def test_primary_navigation_is_task_oriented(self) -> None:
        buttons = re.findall(
            r'class="nav-btn(?: active)?" data-page="([^"]+)">([^<]+)</button>',
            TEMPLATE,
        )
        self.assertEqual(
            buttons,
            [
                ("dashboard", "Dashboard"),
                ("search", "Search VODs"),
                ("queue", "Queue"),
                ("settings", "Settings"),
            ],
        )

    def test_settings_sections_replace_old_primary_pages(self) -> None:
        self.assertEqual(
            re.findall(r'data-settings-tab="([^"]+)"', TEMPLATE),
            ["general", "streamers", "youtube", "advanced"],
        )
        self.assertNotIn('id="page-youtube"', TEMPLATE)
        self.assertNotIn('id="page-localuploads"', TEMPLATE)
        self.assertNotIn('id="page-streamers"', TEMPLATE)

    def test_normal_ui_has_no_desktop_file_manager_actions(self) -> None:
        for label in (
            "Open Download Folder",
            "Show in Folder",
            "Open TXT",
            "YouTube Studio + Show in Folder",
        ):
            self.assertNotIn(label, TEMPLATE)

    def test_queue_exposes_vod_oriented_sections_and_details(self) -> None:
        for heading in ("Running", "Up Next", "Errors"):
            self.assertIn(f">{heading}<", TEMPLATE)
        self.assertIn(">Ready for Upload ", TEMPLATE)
        self.assertIn("<summary>Completed ", TEMPLATE)
        self.assertIn("queueItemsFromJobs", JAVASCRIPT)
        self.assertIn("downloadLogSegment", JAVASCRIPT)
        self.assertIn("Technical details", JAVASCRIPT)
        self.assertNotIn("Expand Details Automatically", TEMPLATE)
        self.assertNotIn("Show All Details", TEMPLATE)
        self.assertIn("queue-vod-item compact", JAVASCRIPT)
        self.assertIn("local-video-table-head", TEMPLATE)

    def test_dashboard_idle_state_is_conditional_and_compact(self) -> None:
        self.assertIn('id="dashboardRunningSection"', TEMPLATE)
        self.assertIn('id="dashboardUpcomingSection"', TEMPLATE)
        self.assertIn("const hasActivity = running.length > 0 || waiting.length > 0", JAVASCRIPT)
        self.assertIn("classList.toggle('hidden', !hasActivity)", JAVASCRIPT)

    def test_search_diagnostics_are_not_in_normal_results(self) -> None:
        self.assertIn("Technical search details", TEMPLATE)
        self.assertIn('id="searchDiagnostics"', TEMPLATE)
        self.assertIn("$('searchErrors').innerHTML = errHtml;", JAVASCRIPT)
        self.assertIn("Ready to Download", JAVASCRIPT)
        self.assertNotIn("New/Pending", JAVASCRIPT)

    def test_ready_for_upload_uses_compact_counts_and_no_manual_move(self) -> None:
        self.assertIn('id="workspacePending" class="heading-count"', TEMPLATE)
        self.assertNotIn('id="workspaceTotal"', TEMPLATE)
        self.assertNotIn('id="workspaceSize"', TEMPLATE)
        self.assertNotIn("Move to Uploaded Archive", JAVASCRIPT)
        self.assertIn("Delete the local VOD file and its sidecars", JAVASCRIPT)

    def test_streamer_editor_preserves_text_storage_contract(self) -> None:
        self.assertIn('id="streamerAddInput"', TEMPLATE)
        self.assertIn('id="streamerEditorList"', TEMPLATE)
        self.assertIn('id="streamersText" class="hidden"', TEMPLATE)
        self.assertIn("setStreamerEditorNames", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"remove\"", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"up\"", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"down\"", JAVASCRIPT)

    def test_settings_finishing_labels_and_youtube_disconnected_copy(self) -> None:
        general = TEMPLATE.split('data-settings-panel="general"', 1)[1].split('data-settings-panel="streamers"', 1)[0]
        advanced = TEMPLATE.split('data-settings-panel="advanced"', 1)[1]
        self.assertNotIn("Concurrent Fragments", general)
        self.assertIn("Concurrent Fragments", advanced)
        self.assertNotIn("After a Batch", TEMPLATE)
        self.assertIn("When to Prepare or Upload", TEMPLATE)
        self.assertIn("Archive Local VOD After Successful Upload", TEMPLATE)
        self.assertIn("YouTube is not connected. Connect your account to enable uploads.", JAVASCRIPT)
        self.assertNotIn("YouTubeNotConnectedError", TEMPLATE + JAVASCRIPT)
        self.assertIn("refreshButton.disabled = !data.connected", JAVASCRIPT)

    def test_single_vod_download_has_one_action(self) -> None:
        self.assertIn('id="singleDownload"', TEMPLATE)
        self.assertNotIn('id="validateSingleVod"', TEMPLATE)
        self.assertIn("await validateSingleVodLink(false)", JAVASCRIPT)
        self.assertIn("VOD added to the download queue.", JAVASCRIPT)

    def test_template_ids_are_unique(self) -> None:
        parser = _IdParser()
        parser.feed(TEMPLATE)
        duplicates = [name for name, count in Counter(parser.ids).items() if count > 1]
        self.assertEqual(duplicates, [])

    def test_responsive_contract_includes_mobile_reflow(self) -> None:
        for breakpoint in ("1050px", "800px", "700px", "430px"):
            self.assertIn(f"max-width:{breakpoint}", STYLESHEET)
        self.assertIn("overflow-x:hidden", STYLESHEET)
        self.assertIn(".sidebar.mobile-open", STYLESHEET)
        self.assertIn(".search-results-table td[data-label]::before", STYLESHEET)


class V11QueueStateRegressionTests(unittest.TestCase):
    def test_single_active_download_stays_running_when_start_marker_was_trimmed(self) -> None:
        jobs = [
            _download_job(
                ["läuft"],
                [
                    "Starting download with the Python module: python -m yt_dlp",
                    "[download]  37.4% of ~2.00GiB at 4.20MiB/s ETA 00:42",
                ],
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["running"])
        self.assertEqual(items[0]["progress"], 37.4)
        self.assertIn("4.20MiB/s", items[0]["extra"])
        self.assertIn("ETA 00:42", items[0]["extra"])

    def test_multi_download_marks_only_the_active_item_running(self) -> None:
        jobs = [
            _download_job(
                ["läuft", "wartet", "wartet"],
                [
                    "--- VOD 1/3 ---",
                    "URL: https://www.twitch.tv/videos/1234567890",
                    "[download]  12.0% of ~2.00GiB at 3.00MiB/s ETA 01:00",
                ],
                count=3,
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["running", "waiting", "waiting"])

    def test_completed_first_item_does_not_mask_second_active_item(self) -> None:
        jobs = [
            _download_job(
                ["fertig", "läuft", "wartet"],
                [
                    "--- VOD 1/3 ---",
                    "VOD 1/3 download completed.",
                    "--- VOD 2/3 ---",
                    "[download]  7.5% of ~2.00GiB at 2.50MiB/s ETA 02:00",
                ],
                count=3,
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["completed", "running", "waiting"])

    def test_successful_completion_is_neither_running_nor_waiting(self) -> None:
        jobs = [
            _download_job(
                ["fertig"],
                ["--- VOD 1/1 ---", "VOD 1/1 download completed."],
                status="fertig",
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["completed"])

    def test_failed_item_is_error_not_running_or_waiting(self) -> None:
        jobs = [
            _download_job(
                ["fehler"],
                [
                    "--- VOD 1/1 ---",
                    "VOD 1/1 ended with error code 1. Continuing with the next VOD.",
                ],
                status="fehler",
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["error"])
        self.assertIn("ended with error code 1", items[0]["error"])

    def test_legacy_job_without_item_statuses_keeps_log_marker_fallback(self) -> None:
        jobs = [
            _download_job(
                None,
                [
                    "--- VOD 1/1 ---",
                    "[download]  9.0% of ~2.00GiB at 1.00MiB/s ETA 03:00",
                ],
            )
        ]

        items = _classify_download_jobs(jobs)

        self.assertEqual([item["state"] for item in items], ["running"])


if __name__ == "__main__":
    unittest.main()
