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


def _classify_download_jobs(
    jobs: list[dict], results: list[dict] | None = None
) -> list[dict]:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Queue state tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function parseProgress');
const end = source.indexOf('function renderQueueVodItem');
if (start < 0 || end < 0 || end <= start) throw new Error('Queue classifier source not found');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const jobs = Array.isArray(input) ? input : input.jobs;
const lastResults = Array.isArray(input) ? [] : (input.results || []);
const localVideoCache = new Map();
function rememberedSearchResults() { return []; }
eval(source.slice(start, end));
const queue = queueItemsFromJobs(jobs);
const displayItems = [
  ...distinguishQueueItems(queue.filter(item => item.state === 'error' && !item.resolved)),
  ...distinguishQueueItems(queue.filter(item => item.state === 'completed').reverse()),
];
const labels = new Map(displayItems.map(item => [queueItemKey(item), item.distinguishingLabel]));
const result = queue.map(item => ({
  index: item.index,
  state: item.state,
  progress: item.progress,
  bytesPerSecond: item.bytesPerSecond ?? null,
  etaSeconds: item.etaSeconds ?? null,
  extra: item.extra,
  error: item.error,
  resolved: item.resolved,
  title: item.title,
  vodId: item.vodId,
  distinguishingLabel: labels.get(queueItemKey(item)) || '',
}));
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps({"jobs": jobs, "results": results or []}),
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_queue_eta(
    jobs: list[dict], now_ms: int = 1_787_082_600_000
) -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Queue ETA tests")
    runner = r"""
process.env.TZ = 'UTC';
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function parseProgress');
const end = source.indexOf('function renderQueueVodItem');
if (start < 0 || end < 0 || end <= start) throw new Error('Queue ETA source not found');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const lastResults = input.results || [];
const localVideoCache = new Map();
function rememberedSearchResults() { return []; }
eval(source.slice(start, end));
const items = queueItemsFromJobs(input.jobs || [], input.nowMs);
process.stdout.write(JSON.stringify({
  items: items.map(item => ({
    state: item.state,
    progress: item.progress ?? null,
    processedSeconds: item.processedSeconds ?? null,
    extra: item.extra,
    etaSeconds: item.etaSeconds ?? null,
  })),
  estimate: overallRunningEstimate(items, input.nowMs),
  durations: [42, 480, 4320].map(formatRemainingDuration),
}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps(
            {"jobs": jobs, "results": [], "nowMs": now_ms}
        ),
        encoding="utf-8",
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


def _upload_job(
    statuses: list[str],
    progresses: list[int | None],
    *,
    errors: list[str] | None = None,
    resolved: list[bool] | None = None,
    status: str = "l\u00e4uft",
) -> dict:
    count = len(statuses)
    return {
        "id": "upload-1",
        "label": "Local YouTube Upload",
        "type": "youtube_upload",
        "status": status,
        "urls": [f"C:/media/vod-{index + 1}.mp4" for index in range(count)],
        "item_statuses": statuses,
        "item_progress": progresses,
        "item_errors": errors or ["" for _ in range(count)],
        "item_resolved": resolved or [False for _ in range(count)],
        "item_metadata": [
            {
                "streamer": "Example",
                "date": "18.08.2026",
                "title": f"Upload VOD {index + 1}",
            }
            for index in range(count)
        ],
        "log": ["YouTube Upload vod-1.mp4: 52%"],
    }


def _render_queue_item_with_saved_open_state(item: dict) -> str:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Queue render tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function renderQueueVodItem');
const end = source.indexOf('function renderQueueGroup');
if (start < 0 || end < 0 || end <= start) throw new Error('Queue renderer source not found');
const queueDetailOpenState = {'youtube_upload:upload-1:0': true};
function escapeHtml(value) { return String(value || ''); }
function niceStatus(value) { return value; }
function renderProgressBar() { return ''; }
function queueErrorSummary(value) { return String(value || ''); }
function queueItemKey(value) { return `${value.job.type || 'download'}:${value.job.id}:${value.index}`; }
eval(source.slice(start, end));
const item = JSON.parse(fs.readFileSync(0, 'utf8'));
const first = renderQueueVodItem(item, true);
const second = renderQueueVodItem(item, true);
process.stdout.write(JSON.stringify({first, second}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps(item),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)["second"]


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

    def test_prepare_metadata_is_secondary_under_actions(self) -> None:
        self.assertNotIn('id="prepareSelectedLocalVideos"', TEMPLATE)
        self.assertNotIn("More actions", JAVASCRIPT)
        self.assertIn("<summary>Actions</summary>", JAVASCRIPT)
        self.assertIn(">Prepare metadata</button>", JAVASCRIPT)
        self.assertNotIn(">Prepare</button>", JAVASCRIPT)

    def test_missing_local_archive_rows_have_no_local_actions(self) -> None:
        self.assertIn("Upload history retained; local actions are unavailable.", JAVASCRIPT)
        self.assertIn("v.local_file_exists !== false", JAVASCRIPT)
        self.assertIn("Size unavailable", JAVASCRIPT)

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
    def test_one_upload_active_has_only_that_vod_running(self) -> None:
        items = _classify_download_jobs(
            [_upload_job(["l\u00e4uft", "wartet"], [52, None])]
        )

        self.assertEqual([item["state"] for item in items], ["running", "waiting"])

    def test_sequential_upload_advances_one_item_at_a_time(self) -> None:
        items = _classify_download_jobs(
            [_upload_job(["fertig", "l\u00e4uft", "wartet"], [100, 17, None])]
        )

        self.assertEqual(
            [item["state"] for item in items],
            ["completed", "running", "waiting"],
        )

    def test_upload_progress_is_not_copied_to_waiting_items(self) -> None:
        items = _classify_download_jobs(
            [_upload_job(["l\u00e4uft", "wartet", "wartet"], [52, None, None])]
        )

        self.assertEqual([item["progress"] for item in items], [52, None, None])

    def test_upload_metadata_identifies_errors_without_unknown_fallbacks(self) -> None:
        items = _classify_download_jobs(
            [
                _upload_job(
                    ["fehler"],
                    [None],
                    errors=["quota exceeded"],
                    status="fehler",
                )
            ]
        )

        self.assertEqual(items[0]["title"], "Upload VOD 1")
        self.assertEqual(items[0]["error"], "quota exceeded")

    def test_resolved_error_remains_failed_but_is_flagged_for_active_filter(self) -> None:
        items = _classify_download_jobs(
            [
                _upload_job(
                    ["fehler"],
                    [None],
                    errors=["quota exceeded"],
                    resolved=[True],
                    status="fehler",
                )
            ]
        )

        self.assertEqual(items[0]["state"], "error")
        self.assertTrue(items[0]["resolved"])
        self.assertIn("item.state === 'error' && !item.resolved", JAVASCRIPT)

    def test_error_detail_open_state_survives_repeated_render(self) -> None:
        html = _render_queue_item_with_saved_open_state(
            {
                "job": {
                    "id": "upload-1",
                    "type": "youtube_upload",
                    "label": "Upload",
                    "log": ["YouTube Upload failed for vod-1.mp4: quota exceeded"],
                },
                "index": 0,
                "state": "error",
                "operation": "YouTube upload failed",
                "streamer": "Example",
                "date": "18.08.2026",
                "title": "Upload VOD 1",
                "error": "quota exceeded",
                "resolved": False,
                "progress": None,
                "extra": "",
            }
        )

        self.assertIn('data-queue-detail-id="youtube_upload:upload-1:0" open', html)
        self.assertIn("Mark as resolved", html)

    def test_completed_metadata_collisions_remain_distinct_by_vod_id(self) -> None:
        job = _download_job(
            ["fertig", "fertig"],
            ["VOD 1/2 download completed.", "VOD 2/2 download completed."],
            status="fertig",
            count=2,
        )
        results = [
            {
                "url": url,
                "streamer": "Example",
                "date": "18.08.2026",
                "title": "Same visible title",
            }
            for url in job["urls"]
        ]

        items = _classify_download_jobs([job], results)

        self.assertEqual(len(items), 2)
        self.assertEqual([item["state"] for item in items], ["completed", "completed"])
        labels = [item["distinguishingLabel"] for item in items]
        self.assertEqual(len(set(labels)), 2)
        self.assertEqual(
            labels,
            ["VOD ID 1234567890", "VOD ID 1234567891"],
        )

    def test_error_metadata_collisions_use_size_and_concise_filename(self) -> None:
        job = _upload_job(
            ["fehler", "fehler"],
            [None, None],
            errors=["failed", "failed"],
            status="fehler",
        )
        for index, metadata in enumerate(job["item_metadata"]):
            metadata.update(
                {
                    "title": "Same visible title",
                    "vod_id": "",
                    "size_bytes": 2 * 1024**3,
                    "name": f"C:/server/private/distinct-vod-{index + 1}.mp4",
                }
            )

        items = _classify_download_jobs([job])

        self.assertEqual(len(items), 2)
        self.assertEqual([item["state"] for item in items], ["error", "error"])
        labels = [item["distinguishingLabel"] for item in items]
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all("2.00 GB" in label for label in labels))
        self.assertIn("distinct-vod-1.mp4", labels[0])
        self.assertIn("distinct-vod-2.mp4", labels[1])
        self.assertTrue(all("C:/server" not in label for label in labels))
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
        self.assertIn("42 sec remaining", items[0]["extra"])

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


class V12QueueEtaRegressionTests(unittest.TestCase):
    @staticmethod
    def upload_job(
        statuses: list[str],
        etas: list[int | None],
        speeds: list[float | None],
        *,
        status: str = "l\u00e4uft",
    ) -> dict:
        job = _upload_job(
            statuses,
            [62 if value == "l\u00e4uft" else None for value in statuses],
            status=status,
        )
        job["item_bytes_uploaded"] = [620_000_000 for _ in statuses]
        job["item_total_bytes"] = [1_000_000_000 for _ in statuses]
        job["item_bytes_per_second"] = speeds
        job["item_eta_seconds"] = etas
        job["item_updated_at"] = [1_787_082_600.0 for _ in statuses]
        return job

    def test_active_upload_displays_speed_and_remaining_time(self) -> None:
        speed = 7.8 * 1024**2
        result = _evaluate_queue_eta(
            [self.upload_job(["l\u00e4uft"], [18 * 60], [speed])]
        )

        self.assertEqual(result["items"][0]["etaSeconds"], 18 * 60)
        self.assertIn("7.8 MB/s", result["items"][0]["extra"])
        self.assertIn("18 min remaining", result["items"][0]["extra"])

    def test_ffmpeg_download_hides_processing_speed_but_keeps_percent_and_eta(self) -> None:
        job = _download_job(["l\u00e4uft"], ["--- VOD 1/1 ---"])
        job.update(
            {
                "item_progress": [72.0],
                "item_processed_seconds": [720.0],
                "item_speed_multiplier": [4.0],
                "item_speed_label": ["4x"],
                "item_eta_seconds": [70],
                "item_updated_at": [1_787_082_600.0],
                "item_total_duration_seconds": [1000.0],
            }
        )

        result = _evaluate_queue_eta([job])

        self.assertEqual(result["items"][0]["progress"], 72)
        self.assertEqual(result["items"][0]["etaSeconds"], 70)
        self.assertEqual(
            result["items"][0]["extra"], "2 min remaining"
        )
        self.assertNotIn("4x", result["items"][0]["extra"])
        self.assertEqual(result["estimate"]["etaSeconds"], 70)

    def test_ffmpeg_download_without_duration_shows_only_truthful_metrics(self) -> None:
        job = _download_job(["l\u00e4uft"], ["--- VOD 1/1 ---"])
        job.update(
            {
                "item_progress": [None],
                "item_processed_seconds": [26436.46],
                "item_speed_multiplier": [61.0],
                "item_speed_label": ["61x"],
                "item_eta_seconds": [None],
                "item_updated_at": [1_787_082_600.0],
                "item_total_duration_seconds": [None],
            }
        )

        result = _evaluate_queue_eta([job])

        self.assertIsNone(result["items"][0]["progress"])
        self.assertIsNone(result["items"][0]["etaSeconds"])
        self.assertEqual(
            result["items"][0]["extra"],
            "7 hrs 20 min processed",
        )
        self.assertNotIn("61x", result["items"][0]["extra"])
        self.assertIsNone(result["estimate"])

    def test_zero_speed_and_insufficient_samples_have_no_eta(self) -> None:
        zero = _evaluate_queue_eta(
            [self.upload_job(["l\u00e4uft"], [None], [0.0])]
        )
        insufficient = _evaluate_queue_eta(
            [self.upload_job(["l\u00e4uft"], [None], [None])]
        )

        self.assertEqual(zero["items"][0]["extra"], "0 B/s")
        self.assertIsNone(zero["estimate"])
        self.assertEqual(insufficient["items"][0]["extra"], "")
        self.assertIsNone(insufficient["estimate"])

    def test_human_readable_eta_covers_seconds_minutes_and_hours(self) -> None:
        result = _evaluate_queue_eta([])

        self.assertEqual(
            result["durations"],
            [
                "42 sec remaining",
                "8 min remaining",
                "1 hr 12 min remaining",
            ],
        )

    def test_one_running_upload_produces_overall_completion(self) -> None:
        now_ms = (19 * 3600 + 50 * 60) * 1000
        job = self.upload_job(["l\u00e4uft"], [24 * 60], [1024**2])
        job["item_updated_at"] = [now_ms / 1000]
        result = _evaluate_queue_eta([job], now_ms=now_ms)

        self.assertEqual(result["estimate"]["etaSeconds"], 24 * 60)
        self.assertEqual(
            result["estimate"]["completionLabel"],
            "Estimated completion 20:14",
        )
        self.assertEqual(
            result["estimate"]["remainingLabel"],
            "24 min remaining",
        )

    def test_completion_clock_stays_stable_between_backend_samples(self) -> None:
        sample_ms = (19 * 3600 + 50 * 60) * 1000
        job = self.upload_job(["l\u00e4uft"], [24 * 60], [1024**2])
        job["item_updated_at"] = [sample_ms / 1000]

        result = _evaluate_queue_eta(
            [job], now_ms=sample_ms + 2 * 60 * 1000
        )

        self.assertEqual(result["items"][0]["etaSeconds"], 22 * 60)
        self.assertEqual(
            result["estimate"]["completionLabel"],
            "Estimated completion 20:14",
        )

    def test_concurrent_download_and_upload_use_longest_eta(self) -> None:
        download = _download_job(
            ["l\u00e4uft"],
            [
                "--- VOD 1/1 ---",
                "[download] 42.0% of 2.00GiB at 4.20MiB/s ETA 00:42",
            ],
        )
        upload = self.upload_job(
            ["l\u00e4uft"], [2 * 60], [2 * 1024**2]
        )

        result = _evaluate_queue_eta([download, upload])

        self.assertEqual(result["estimate"]["etaSeconds"], 2 * 60)
        self.assertEqual(
            sorted(item["etaSeconds"] for item in result["items"]),
            [42, 120],
        )

    def test_waiting_items_are_excluded_from_overall_eta(self) -> None:
        result = _evaluate_queue_eta(
            [
                self.upload_job(
                    ["l\u00e4uft", "wartet"],
                    [60, 600],
                    [1024**2, 1024**2],
                )
            ]
        )

        self.assertEqual(result["estimate"]["etaSeconds"], 60)
        self.assertEqual(
            [item["etaSeconds"] for item in result["items"]],
            [60, None],
        )

    def test_completed_and_failed_items_do_not_contribute(self) -> None:
        completed = self.upload_job(
            ["fertig"], [600], [1024**2], status="fertig"
        )
        failed = self.upload_job(
            ["fehler"], [900], [1024**2], status="fehler"
        )

        result = _evaluate_queue_eta([completed, failed])

        self.assertIsNone(result["estimate"])
        self.assertEqual(
            [item["etaSeconds"] for item in result["items"]],
            [None, None],
        )

    def test_eta_ui_is_responsive_and_uses_no_tilde_notation(self) -> None:
        self.assertIn('id="queueRunningEstimate"', TEMPLATE)
        self.assertIn(".running-estimate", STYLESHEET)
        self.assertIn("min-width:0", STYLESHEET)
        self.assertIn("max-width:430px", STYLESHEET)
        self.assertNotIn("~", TEMPLATE + JAVASCRIPT + STYLESHEET)


if __name__ == "__main__":
    unittest.main()
