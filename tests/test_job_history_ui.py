from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
STYLESHEET = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _evaluate_history_ui(jobs: list[dict]) -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Queue history UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const classifierStart = source.indexOf('function parseProgress');
const classifierEnd = source.indexOf('function renderQueueGroup');
const historyStart = source.indexOf('function queueHistoryTimestamp');
const historyEnd = source.indexOf('function renderQueuePersistenceStatus');
const errorStart = source.indexOf('function friendlyQueueActionError');
const errorEnd = source.indexOf('function renderQueueLaneControls');
if ([classifierStart, classifierEnd, historyStart, historyEnd, errorStart, errorEnd].some(value => value < 0)) throw new Error('Queue history helpers not found');
const lastResults = [];
const localVideoCache = new Map();
const queueDetailOpenState = {};
function rememberedSearchResults() { return []; }
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  })[character]);
}
function renderProgressBar() { return ''; }
eval(source.slice(classifierStart, classifierEnd));
eval(source.slice(historyStart, historyEnd));
eval(source.slice(errorStart, errorEnd));
const items = queueItemsFromJobs(input.jobs || [], Date.parse('2026-08-23T21:00:00Z'));
const rendered = items.map(item => ({
  jobId:String(item.job.id), itemId:String(item.itemId), state:item.state,
  type:item.job.type || 'download', html:renderQueueVodItem(item, true),
}));
const attention = queueHistoryNewest(items.filter(item => ['error', 'interrupted'].includes(item.state)));
const completed = queueHistoryNewest(items.filter(item => item.state === 'completed'));
const active = items.filter(item => ['running', 'waiting'].includes(item.state));
const reasons = [
  'source_missing', 'source_changed', 'unsafe_source_path', 'review_required',
  'persistence_unavailable', 'persistence_validation_failed',
  'recording_retry_unsupported', 'already_retried', 'not_retryable'
];
process.stdout.write(JSON.stringify({
  rendered,
  attentionOrder:attention.map(item => String(item.job.id)),
  completedOrder:completed.map(item => String(item.job.id)),
  activeOrder:active.map(item => String(item.job.id)),
  friendly:Object.fromEntries(reasons.map(reason => [reason, friendlyQueueActionError({reason, message:'C:/private/raw exception'})])),
}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps({"jobs": jobs}),
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_persistence_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for persistence UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function renderQueuePersistenceStatus');
const end = source.indexOf('function renderVodQueue');
if (start < 0 || end < 0) throw new Error('Persistence renderer not found');
const box = {
  innerHTML:'', hidden:true,
  classList:{add(name){if(name === 'hidden') box.hidden=true;}, remove(name){if(name === 'hidden') box.hidden=false;}}
};
function $(id) { return id === 'queuePersistenceWarning' ? box : null; }
eval(source.slice(start, end));
function render(status) {
  renderQueuePersistenceStatus(status);
  return {html:box.innerHTML, hidden:box.hidden};
}
const storeless = render({enabled:false, healthy:null});
const healthy = render({enabled:true, healthy:true});
const current = render({enabled:true, healthy:false, current_degraded:true});
const recovered = render({enabled:true, healthy:true});
const history = render({enabled:true, healthy:true, history_degraded:true});
const storelessAfterWarning = render({enabled:false, healthy:null});
process.stdout.write(JSON.stringify({
  storeless, healthy, current, recovered, history, storelessAfterWarning,
}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _capability(*, retry=False, retry_job_id="", blocked="") -> dict:
    return {
        "can_cancel": False,
        "can_remove": False,
        "can_retry": retry,
        "can_resolve": False,
        "can_stop_after_current": False,
        "retry_job_id": retry_job_id,
        "retry_blocked_reason": blocked,
        "retry_block_reason": "",
    }


def _download_job(
    job_id: str,
    state: str,
    *,
    reason: str = "",
    updated: str = "2026-08-23T20:00:00Z",
    retry: bool = False,
    retry_job_id: str = "",
) -> dict:
    legacy = {"queued": "wartet", "running": "läuft", "completed": "fertig"}.get(
        state, "fehler"
    )
    return {
        "id": job_id,
        "label": f"Download {job_id}",
        "type": "download",
        "state": state,
        "status": legacy,
        "created_at": "2026-08-23T19:00:00Z",
        "updated_at": updated,
        "finished_at": updated if state not in {"queued", "running"} else None,
        "urls": [f"https://www.twitch.tv/videos/{1234560000 + int(job_id)}"],
        "item_ids": [f"{job_id}-item-1"],
        "item_states": [state],
        "item_statuses": [legacy],
        "item_progress": [63],
        "item_processed_seconds": [3600],
        "item_speed_label": ["43.2x"],
        "item_eta_seconds": [240],
        "item_updated_at": [updated],
        "item_completion_reasons": [reason],
        "item_recovery_reasons": [reason],
        "item_failure_kinds": [""],
        "item_capabilities": [
            _capability(retry=retry, retry_job_id=retry_job_id)
        ],
        "log": [],
    }


class JobHistoryUiTests(unittest.TestCase):
    def setUp(self):
        jobs = [
            _download_job("12", "completed", updated="2026-08-23T20:20:00Z"),
            _download_job("11", "completed", updated="2026-08-23T20:10:00Z"),
            _download_job("10", "interrupted", reason="worker_shutdown", updated="2026-08-23T20:40:00Z", retry=True),
            _download_job("9", "interrupted", reason="restart_before_start", updated="2026-08-23T20:30:00Z", retry=True),
            {
                "id": "8", "label": "Upload batch", "type": "youtube_upload",
                "state": "interrupted", "status": "fehler",
                "created_at": "2026-08-23T19:00:00Z", "updated_at": "2026-08-23T20:50:00Z", "finished_at": "2026-08-23T20:50:00Z",
                "urls": ["C:/media/safe.mp4", "C:/media/uncertain.mp4", "C:/media/failed.mp4"],
                "item_ids": ["8-item-1", "8-item-2", "8-item-3"],
                "item_states": ["interrupted", "interrupted", "failed"],
                "item_statuses": ["fehler", "fehler", "fehler"],
                "item_progress": [0, 71, 15],
                "item_bytes_uploaded": [0, 710, 150], "item_total_bytes": [1000, 1000, 1000],
                "item_bytes_per_second": [None, 300, 200], "item_eta_seconds": [None, 1, 4], "item_updated_at": [None, "2026-08-23T20:49:00Z", "2026-08-23T20:48:00Z"],
                "item_errors": ["", "", "Upload rejected"],
                "item_resolved": [False, False, False],
                "item_completion_reasons": ["restart_before_start", "upload_status_unknown", "failed"],
                "item_recovery_reasons": ["restart_before_start", "upload_status_unknown", ""],
                "item_failure_kinds": ["", "uncertain", "known"],
                "item_capabilities": [_capability(retry=True), _capability(blocked="review_required"), _capability(retry=True)],
                "item_metadata": [
                    {"streamer": "SameStreamer", "title": "Safe upload"},
                    {"streamer": "SameStreamer", "title": "Uncertain upload"},
                    {"streamer": "SameStreamer", "title": "Failed upload"},
                ],
                "log": [],
            },
            {
                "id": "7", "label": "Live recording", "type": "recording", "streamer": "nika_livetv", "title": "Live title",
                "state": "interrupted", "status": "fehler", "origin": "auto", "attempt": 2, "recorded_seconds": 5077,
                "created_at": "2026-08-23T19:00:00Z", "updated_at": "2026-08-23T20:45:00Z", "finished_at": "2026-08-23T20:45:00Z",
                "urls": ["nika_livetv"], "item_ids": ["7-item-1"], "item_states": ["interrupted"], "item_statuses": ["fehler"],
                "item_completion_reasons": ["restart_interrupted"], "item_recovery_reasons": ["restart_interrupted"], "item_failure_kinds": [""],
                "item_capabilities": [_capability(blocked="recording_retry_unsupported")], "log": [],
            },
            {
                "id": "6", "label": "Old completed recording", "type": "recording", "streamer": "nika_livetv", "state": "completed", "status": "fertig",
                "urls": ["nika_livetv"], "item_ids": ["6-item-1"], "item_states": ["completed"], "item_statuses": ["fertig"], "item_capabilities": [_capability()], "log": [],
            },
            _download_job("5", "interrupted", reason="restart_interrupted", updated="2026-08-23T20:05:00Z", retry_job_id="13"),
            _download_job("4", "running", updated="2026-08-23T20:55:00Z"),
            _download_job("3", "queued", updated="2026-08-23T20:00:00Z"),
        ]
        self.result = _evaluate_history_ui(jobs)

    def item(self, job_id: str, item_id: str | None = None) -> dict:
        for item in self.result["rendered"]:
            if item["jobId"] == job_id and (
                item_id is None or item["itemId"] == item_id
            ):
                return item
        raise AssertionError(f"Missing item {job_id}/{item_id}")

    def normal_row(self, job_id: str, item_id: str | None = None) -> str:
        return self.item(job_id, item_id)["html"].split("<details", 1)[0]

    def test_interrupted_download_wording_retry_and_static_progress(self):
        running = self.item("10")["html"]
        before_start = self.item("9")["html"]
        self.assertIn("Download interrupted", running)
        self.assertIn("while this download was running", running)
        self.assertIn("before this download started", before_start)
        self.assertIn('data-queue-action="retry"', running)
        self.assertNotIn("worker_shutdown", running.split("<details", 1)[0])
        self.assertIn("Last recorded progress", running)
        self.assertNotIn("43.2x", running)
        self.assertNotIn("remaining", running)

    def test_safe_and_uncertain_uploads_have_distinct_actions(self):
        safe = self.normal_row("8", "8-item-1")
        uncertain = self.normal_row("8", "8-item-2")
        failed = self.normal_row("8", "8-item-3")
        self.assertIn("Upload interrupted", safe)
        self.assertIn("before the YouTube upload started", safe)
        self.assertIn('data-queue-action="retry"', safe)
        self.assertIn("Upload status uncertain", uncertain)
        self.assertIn("Check YouTube Studio", uncertain)
        self.assertIn("Review required", uncertain)
        self.assertNotIn('data-queue-action="retry"', uncertain)
        self.assertIn("Failed", failed)
        self.assertIn('data-queue-action="retry"', failed)
        self.assertNotIn("upload_status_unknown", uncertain)
        self.assertNotIn("review_required", uncertain)

    def test_interrupted_recording_is_discoverable_without_retry(self):
        recording = self.item("7")["html"]
        self.assertIn("Recording interrupted", recording)
        self.assertIn("while this recording was active", recording)
        self.assertNotIn('data-queue-action="retry"', recording)
        self.assertNotIn("6", [item["jobId"] for item in self.result["rendered"]])

    def test_already_retried_item_shows_relationship_not_action(self):
        html = self.normal_row("5")
        self.assertIn("Retry started as Job 13", html)
        self.assertNotIn('data-queue-action="retry"', html)

    def test_history_order_is_newest_first_and_active_order_is_unchanged(self):
        self.assertEqual(self.result["completedOrder"], ["12", "11"])
        self.assertEqual(
            self.result["attentionOrder"][:5], ["8", "8", "8", "7", "10"]
        )
        self.assertEqual(self.result["activeOrder"], ["3", "4"])

    def test_retry_errors_are_safe_and_stable(self):
        friendly = self.result["friendly"]
        self.assertIn("no longer available", friendly["source_missing"])
        self.assertIn("changed", friendly["source_changed"])
        self.assertIn("safely", friendly["unsafe_source_path"])
        self.assertIn("YouTube Studio", friendly["review_required"])
        self.assertIn("persistence", friendly["persistence_unavailable"])
        self.assertNotIn("C:/private", str(friendly))

    def test_persistence_health_only_warns_for_degradation(self):
        values = _evaluate_persistence_ui()
        self.assertTrue(values["storeless"]["hidden"])
        self.assertEqual(values["storeless"]["html"], "")
        self.assertTrue(values["healthy"]["hidden"])
        self.assertEqual(values["healthy"]["html"], "")
        self.assertFalse(values["current"]["hidden"])
        self.assertIn("Job history persistence degraded", values["current"]["html"])
        self.assertIn("Current work can continue", values["current"]["html"])
        self.assertTrue(values["recovered"]["hidden"])
        self.assertEqual(values["recovered"]["html"], "")
        self.assertFalse(values["history"]["hidden"])
        self.assertIn("could not be restored", values["history"]["html"])
        self.assertIn(
            "Current downloads and uploads can continue normally",
            values["history"]["html"],
        )
        self.assertTrue(values["storelessAfterWarning"]["hidden"])
        self.assertEqual(values["storelessAfterWarning"]["html"], "")

    def test_persistence_warning_has_no_empty_desktop_or_mobile_footprint(self):
        self.assertIn(
            'id="queuePersistenceWarning" class="queue-persistence-warning" role="status" aria-live="polite" hidden',
            TEMPLATE,
        )
        hidden_rule = ".queue-persistence-warning[hidden] { display:none; }"
        self.assertIn(hidden_rule, STYLESHEET)

    def test_template_has_accessible_history_confirmation_and_persistent_empty_copy(self):
        self.assertIn('id="clearCompletedDialog"', TEMPLATE)
        self.assertIn('aria-labelledby="clearCompletedDialogTitle"', TEMPLATE)
        self.assertIn("Downloaded and uploaded media are not deleted.", TEMPLATE)
        self.assertNotIn("completed in this session", TEMPLATE.lower())
        self.assertNotIn("cancelled in this session", TEMPLATE.lower())
        self.assertLess(TEMPLATE.index("Running"), TEMPLATE.index("Needs Attention"))
        self.assertIn("@media (max-width:430px)", STYLESHEET)
        self.assertIn("min-height:44px", STYLESHEET)


if __name__ == "__main__":
    unittest.main()
