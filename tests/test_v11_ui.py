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


def _evaluate_playlist_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for playlist UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function canonicalStreamerLoginClient');
const end = source.indexOf('function streamerEditorNames');
if (start < 0 || end < 0 || end <= start) throw new Error('Playlist UI helpers not found');
let youtubePlaylistChoices = [];
function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  })[character]);
}
eval(source.slice(start, end));
const profiles = {
  digitalgirluli: {youtube_playlist_id:'PLAYLIST_A', auto_record:true, auto_vod_download:true, auto_youtube_upload:true},
  auto_only: {auto_record:true},
  auto_vod_only: {auto_vod_download:true},
  auto_youtube_only: {auto_youtube_upload:true},
  false_only: {auto_record:false},
  string_true: {auto_record:'true'},
  orphan_streamer: {youtube_playlist_id:'ORPHAN'}
};
const clonedProfiles = cloneStreamerProfiles(profiles);
const setOverride = withStreamerPlaylistSelection(
  profiles, 'DigitalGirlUli', 'PLAYLIST_B'
);
const removedOverride = withStreamerPlaylistSelection(
  profiles, '@DigitalGirlUli', ''
);
const autoEnabled = withStreamerAutoRecordSelection(
  withStreamerPlaylistSelection({}, 'DigitalGirlUli', 'PLAYLIST_A'),
  'DigitalGirlUli', true
);
const playlistChangedAfterAuto = withStreamerPlaylistSelection(
  autoEnabled, 'DigitalGirlUli', 'PLAYLIST_B'
);
const autoRemovedAfterPlaylist = withStreamerAutoRecordSelection(
  playlistChangedAfterAuto, 'DigitalGirlUli', false
);
const autoEnabledAfterVod = withStreamerAutoRecordSelection(
  {digitalgirluli:{youtube_playlist_id:'PLAYLIST_A', auto_vod_download:true}},
  'DigitalGirlUli', true
);
const autoRemovedAfterVod = withStreamerAutoRecordSelection(
  autoEnabledAfterVod, 'DigitalGirlUli', false
);
const emptyProfileRemoved = withStreamerAutoRecordSelection(
  {auto_only:{auto_record:true}}, 'auto_only', false
);
const autoYoutubeEnabled = withStreamerAutoYoutubeSelection(
  {digitalgirluli:{youtube_playlist_id:'PLAYLIST_A', auto_vod_download:true}},
  'DigitalGirlUli', true
);
const autoYoutubeRemoved = withStreamerAutoYoutubeSelection(
  autoYoutubeEnabled, 'DigitalGirlUli', false
);
const emptyAutoYoutubeProfileRemoved = withStreamerAutoYoutubeSelection(
  {auto_youtube_only:{auto_youtube_upload:true}}, 'auto_youtube_only', false
);
process.stdout.write(JSON.stringify({
  clonedProfiles,
  configured: streamerProfilePlaylistId(profiles, 'DigitalGirlUli'),
  inherited: streamerProfilePlaylistId(profiles, 'NoOverride'),
  autoConfigured: streamerProfileAutoRecordEnabled(profiles, 'DigitalGirlUli'),
  autoMissing: streamerProfileAutoRecordEnabled(profiles, 'NoOverride'),
  autoYoutubeConfigured: streamerProfileAutoYoutubeEnabled(profiles, 'DigitalGirlUli'),
  autoYoutubeMissing: streamerProfileAutoYoutubeEnabled(profiles, 'NoOverride'),
  configuredOptions: playlistOptionsHtml('PLAYLIST_A', 'Global Default'),
  inheritedOptions: playlistOptionsHtml('', 'Global Default'),
  setOverride,
  removedOverride,
  autoEnabled,
  playlistChangedAfterAuto,
  autoRemovedAfterPlaylist,
  autoEnabledAfterVod,
  autoRemovedAfterVod,
  emptyProfileRemoved,
  autoYoutubeEnabled,
  autoYoutubeRemoved,
  emptyAutoYoutubeProfileRemoved,
  defaultSingle: buildYoutubeUploadRequest(['one.mp4'], 'streamer-default'),
  defaultMultiple: buildYoutubeUploadRequest(
    ['one.mp4', 'two.mp4'], 'streamer-default'
  ),
  noPlaylistSingle: buildYoutubeUploadRequest(['one.mp4'], 'no-playlist'),
  noPlaylistMultiple: buildYoutubeUploadRequest(
    ['one.mp4', 'two.mp4'], 'no-playlist'
  ),
  explicitSingle: buildYoutubeUploadRequest(
    ['one.mp4'], 'playlist', 'PLAYLIST_A'
  ),
  explicitMultiple: buildYoutubeUploadRequest(
    ['one.mp4', 'two.mp4'], 'playlist', 'PLAYLIST_A'
  )
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


def _search_result_status(result: dict) -> str:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for search UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function searchResultStatusHtml');
const end = source.indexOf('function renderResults', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Search status helper not found');
eval(source.slice(start, end));
process.stdout.write(searchResultStatusHtml(JSON.parse(fs.readFileSync(0, 'utf8'))));
"""
    completed = subprocess.run(
        [NODE, "-e", runner], input=json.dumps(result), cwd=ROOT,
        encoding="utf-8", capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


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
  key: queueItemKey(item),
  detailLogs: item.detailLogs || [],
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
    job["item_ids"] = [f"1-item-{index + 1}" for index in range(item_count)]
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
        "item_ids": [f"upload-1-item-{index + 1}" for index in range(count)],
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
const start = source.indexOf('function queueRecoveryPresentation');
const end = source.indexOf('function renderQueueGroup');
if (start < 0 || end < 0 || end <= start) throw new Error('Queue renderer source not found');
const queueDetailOpenState = {'youtube_upload:upload-1:upload-1-item-1': true};
function escapeHtml(value) { return String(value || ''); }
function niceStatus(value) { return value; }
function renderProgressBar() { return ''; }
function formatProcessedDuration(value) { return `${value}s`; }
function queueErrorSummary(value) { return String(value || ''); }
function queueItemKey(value) { return `${value.job.type || 'download'}:${value.job.id}:${value.itemId || value.index}`; }
eval(source.slice(start, end));
const item = JSON.parse(fs.readFileSync(0, 'utf8'));
item.itemId = item.itemId || 'upload-1-item-1';
item.capabilities = item.capabilities || {};
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


def _evaluate_local_history_ui(card: dict, videos: list[dict]) -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for local-history UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function workspaceStatusClass');
const end = source.indexOf('async function loadLocalVideos');
if (start < 0 || end < 0 || end <= start) throw new Error('Local-history renderer source not found');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const UPLOADED_HISTORY_PAGE_SIZE = 20;
const localVideoCache = new Map();
function escapeHtml(value) { return String(value || ''); }
eval(source.slice(start, end));
const visible = visibleLocalVideoRows(input.videos || [], true, 20);
process.stdout.write(JSON.stringify({
  card: renderLocalVideoCard(input.card),
  visiblePaths: visible.map(video => video.path),
  sourceCount: (input.videos || []).length,
}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner],
        cwd=ROOT,
        input=json.dumps({"card": card, "videos": videos}),
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_live_stream_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for Live Streams UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function streamerProfileAutoRecordEnabled');
const end = source.indexOf('function streamerEditorNames');
if (start < 0 || end < 0 || end <= start) throw new Error('Live Streams UI helpers not found');
const ACTIVE_RECORDING_STATES = new Set(['queued', 'running', 'stopping']);
const LIVE_STATUS_CONCURRENCY = 2;
let liveStreamers = [];
let liveStreamStatuses = new Map();
let liveStatusRequests = new Map();
let liveStatusRefreshPromise = null;
let liveStatusInitialRefreshStarted = false;
let liveOfflineExpanded = false;
let liveStatusLastUpdatedAt = null;
let liveRecordingJobs = [];
let liveRecordingActions = new Map();
let autoRecorderStatusSnapshot = null;
let state = {settings:{auto_recorder_enabled:false, streamer_profiles:{}}};
const elements = {
  liveStreamsList: {
    innerHTML:'',
    querySelectorAll:() => [],
    querySelector:() => null
  },
  liveStreamsSummary: {textContent:''},
  liveStreamsRefreshStatus: {textContent:''},
  refreshLiveStatuses: {disabled:false}
};
const $ = id => elements[id] || null;
function canonicalStreamerLoginClient(value) {
  const login = String(value || '').trim().replace(/^@+/, '').toLowerCase();
  return /^[a-z0-9_]{1,25}$/.test(login) ? login : '';
}
function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, character => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  })[character]);
}
let calls = [];
let apiHandler = async () => ({});
async function api(path, options={}) {
  calls.push({path, options});
  return apiHandler(path, options);
}
let pollMode = 'resolved';
let pollCalls = 0;
function pollJobs() {
  pollCalls += 1;
  return pollMode === 'never' ? new Promise(() => {}) : Promise.resolve();
}
function showToast() {}
eval(source.slice(start, end));

(async () => {
  syncLiveStreamers(['Nika_LiveTV', 'DigitalGirlUli', 'Bearykchen', 'Xerax_TTV']);
  const configuredHtml = elements.liveStreamsList.innerHTML;
  const configuredSummary = elements.liveStreamsSummary.textContent;

  liveStreamStatuses = new Map([
    ['nika_livetv', {state:'live', streamer:'nika_livetv', title:'First confirmed live'}],
    ['digitalgirluli', {state:'offline', streamer:'digitalgirluli'}],
    ['bearykchen', {state:'checking', streamer:'bearykchen'}],
    ['xerax_ttv', {state:'unknown', streamer:'xerax_ttv'}]
  ]);
  renderLiveStreams();
  const partialInitialHtml = elements.liveStreamsList.innerHTML;
  const partialInitialSummary = elements.liveStreamsSummary.textContent;

  liveStreamStatuses.set('nika_livetv', {
    state:'live', streamer:'nika_livetv', title:'Synthetic stream title',
    started_at:'2026-08-23T20:14:00Z'
  });
  liveRecordingJobs = [];
  const liveHtml = renderLiveStreamCard('Nika_LiveTV');
  liveStreamStatuses.set('nika_livetv', {state:'offline', streamer:'nika_livetv'});
  const offlineHtml = renderOfflineStreamer('Nika_LiveTV');
  liveStreamStatuses.set('nika_livetv', {state:'error', streamer:'nika_livetv'});
  const errorHtml = renderLiveStreamCard('Nika_LiveTV');

  const statuses = {
    queued: recordingStatusText({state:'queued'}),
    running: recordingStatusText({state:'running', recorded_seconds:5077}),
    stopping: recordingStatusText({state:'stopping'}),
    natural: recordingStatusText({state:'completed', completion_reason:'natural_end', output_complete:true}),
    stopped: recordingStatusText({state:'completed', completion_reason:'stopped_by_user', output_complete:true}),
    processError: recordingStatusText({state:'failed', completion_reason:'process_error'}),
    incomplete: recordingStatusText({state:'failed', completion_reason:'stop_incomplete'}),
    stopFailed: recordingStatusText({state:'failed', completion_reason:'stop_failed'})
  };

  liveStreamStatuses = new Map([
    ['nika_livetv', {state:'live', streamer:'nika_livetv', title:'Recording stream', started_at:'2026-08-23T20:14:00Z'}],
    ['digitalgirluli', {state:'live', streamer:'digitalgirluli', title:'Second live stream', started_at:'2026-08-23T20:20:00Z'}],
    ['bearykchen', {state:'offline', streamer:'bearykchen'}],
    ['xerax_ttv', {state:'offline', streamer:'xerax_ttv'}]
  ]);
  liveRecordingJobs = [{
    id:'recording-7', type:'recording', streamer:'nika_livetv', state:'running',
    recorded_seconds:5077, title:'Recording title'
  }];
  renderLiveStreams();
  const hierarchyHtml = elements.liveStreamsList.innerHTML;
  const summaryText = elements.liveStreamsSummary.textContent;
  const collapsedHtml = elements.liveStreamsList.innerHTML;
  toggleOfflineStreamers();
  const expandedHtml = elements.liveStreamsList.innerHTML;
  toggleOfflineStreamers();
  const recollapsedHtml = elements.liveStreamsList.innerHTML;

  liveStreamStatuses.set('nika_livetv', {state:'live', streamer:'nika_livetv'});
  liveRecordingJobs = [{
    id:'recording-complete', type:'recording', streamer:'nika_livetv',
    state:'completed', completion_reason:'stopped_by_user', output_complete:true
  }];
  const stoppedSavedHtml = renderLiveStreamCard('Nika_LiveTV');
  liveRecordingJobs = [{
    id:'recording-natural', type:'recording', streamer:'nika_livetv',
    state:'completed', completion_reason:'natural_end', output_complete:true
  }];
  const naturalSavedHtml = renderLiveStreamCard('Nika_LiveTV');
  liveRecordingJobs = [{
    id:'recording-failed', type:'recording', streamer:'nika_livetv',
    state:'failed', completion_reason:'stop_incomplete', output_complete:false
  }];
  const failedRecordingHtml = renderLiveStreamCard('Nika_LiveTV');

  liveStreamStatuses.set('nika_livetv', {state:'error', streamer:'nika_livetv'});
  liveRecordingJobs = [{
    id:'recording-7', type:'recording', streamer:'nika_livetv', state:'running',
    recorded_seconds:5077, title:'Recording title'
  }];
  const recordingOverridesErrorHtml = renderLiveStreamCard('Nika_LiveTV');
  liveStreamStatuses.set('digitalgirluli', {
    state:'live', streamer:'digitalgirluli', title:'Another live stream'
  });
  const otherStreamerHtml = renderLiveStreamCard('DigitalGirlUli');

  state = {settings:{
    auto_recorder_enabled:true,
    streamer_profiles:{nika_livetv:{auto_record:true}}
  }};
  autoRecorderStatusSnapshot = {enabled:true, running:true, state_healthy:true, phase:'sleeping', watched_count:1};
  liveStreamStatuses.set('nika_livetv', {state:'live', streamer:'nika_livetv'});
  liveRecordingJobs = [{
    id:'recording-auto', type:'recording', streamer:'nika_livetv', state:'running',
    origin:'auto', recorded_seconds:42, title:'Auto recording'
  }];
  const autoStartedHtml = renderLiveStreamCard('Nika_LiveTV');
  liveRecordingJobs = [];
  state.settings.auto_recorder_enabled = false;
  const autoPausedLiveHtml = renderLiveStreamCard('Nika_LiveTV');
  state.settings.auto_recorder_enabled = true;
  const autoEnabledLiveHtml = renderLiveStreamCard('Nika_LiveTV');
  autoRecorderStatusSnapshot = {enabled:true, running:true, state_healthy:false, phase:'degraded', watched_count:1};
  const autoDegradedLiveHtml = renderLiveStreamCard('Nika_LiveTV');
  autoRecorderStatusSnapshot = {enabled:true, running:false, state_healthy:null, phase:'stopped', watched_count:1};
  const autoUnavailableLiveHtml = renderLiveStreamCard('Nika_LiveTV');

  const autoRecorderViews = {
    loading:autoRecorderStatusPresentation(null),
    running:autoRecorderStatusPresentation({enabled:true, running:true, state_healthy:true, phase:'sleeping', watched_count:4, last_check_completed_at:'2026-08-23T15:42:00Z'}),
    paused:autoRecorderStatusPresentation({enabled:false, running:true, state_healthy:null, phase:'paused', watched_count:4}),
    zero:autoRecorderStatusPresentation({enabled:true, running:true, state_healthy:true, phase:'sleeping', watched_count:0}),
    degraded:autoRecorderStatusPresentation({enabled:true, running:true, state_healthy:false, phase:'degraded', watched_count:4, last_error_code:'invalid_json'}),
    failed:autoRecorderStatusPresentation({unavailable:true}),
    native:autoRecorderStatusPresentation({enabled:true, running:false, state_healthy:null, phase:'stopped', watched_count:4})
  };

  liveStreamers = ['Nika_LiveTV', 'DigitalGirlUli'];
  liveRecordingJobs = [];
  liveStreamStatuses = new Map([
    ['nika_livetv', {state:'offline', streamer:'nika_livetv'}],
    ['digitalgirluli', {state:'offline', streamer:'digitalgirluli'}]
  ]);
  liveOfflineExpanded = false;
  renderLiveStreams();
  const noLiveHtml = elements.liveStreamsList.innerHTML;

  calls = [];
  liveRecordingJobs = [];
  liveRecordingActions = new Map();
  liveStreamers = ['Nika_LiveTV', 'DigitalGirlUli'];
  liveStreamStatuses = new Map();
  const refreshResolvers = [];
  apiHandler = path => new Promise(resolve => refreshResolvers.push(() => resolve({
    streamer:path.includes('nika_livetv') ? 'nika_livetv' : 'digitalgirluli',
    state:path.includes('nika_livetv') ? 'live' : 'offline',
    title:'Refreshed title'
  })));
  const refreshPromise = refreshLiveStatuses();
  await Promise.resolve();
  const updatingMessage = elements.liveStreamsRefreshStatus.textContent;
  const updatingDisabled = elements.refreshLiveStatuses.disabled;
  const initialRefreshHtml = elements.liveStreamsList.innerHTML;
  const updatedBeforeFirstResult = liveStatusLastUpdatedAt;
  refreshResolvers[0]();
  await new Promise(resolve => setImmediate(resolve));
  const oneResultMessage = elements.liveStreamsRefreshStatus.textContent;
  const oneResultHtml = elements.liveStreamsList.innerHTML;
  const updatedAfterFirstResult = liveStatusLastUpdatedAt;
  refreshResolvers[1]();
  await refreshPromise;
  const refreshedDisabled = elements.refreshLiveStatuses.disabled;
  const refreshCalls = calls.filter(call => call.path.startsWith('/api/live/status'));
  const updatedAfterRefresh = liveStatusLastUpdatedAt;

  calls = [];
  liveStreamers = ['Nika_LiveTV', 'DigitalGirlUli'];
  liveStreamStatuses = new Map([
    ['nika_livetv', {state:'live', streamer:'nika_livetv', title:'Previously live'}],
    ['digitalgirluli', {state:'offline', streamer:'digitalgirluli'}]
  ]);
  liveStatusLastUpdatedAt = new Date('2026-08-23T20:00:00Z');
  liveOfflineExpanded = true;
  const manualResolvers = new Map();
  apiHandler = path => new Promise(resolve => manualResolvers.set(path, resolve));
  const manualRefreshPromise = refreshLiveStatuses();
  await Promise.resolve();
  const manualPendingHtml = elements.liveStreamsList.innerHTML;
  const manualPendingSummary = elements.liveStreamsSummary.textContent;
  manualResolvers.get('/api/live/status?streamer=nika_livetv')({
    streamer:'nika_livetv', state:'offline'
  });
  await new Promise(resolve => setImmediate(resolve));
  const manualAfterFirstHtml = elements.liveStreamsList.innerHTML;
  const manualAfterFirstSummary = elements.liveStreamsSummary.textContent;
  manualResolvers.get('/api/live/status?streamer=digitalgirluli')({
    streamer:'digitalgirluli', state:'live', title:'Newly live'
  });
  await manualRefreshPromise;
  const manualFinalHtml = elements.liveStreamsList.innerHTML;
  const manualFinalSummary = elements.liveStreamsSummary.textContent;

  liveStreamers = ['Nika_LiveTV'];
  liveStatusLastUpdatedAt = null;
  liveStreamStatuses = new Map([['nika_livetv', {state:'unknown', streamer:'nika_livetv'}]]);
  apiHandler = async () => { throw new Error('synthetic initial failure'); };
  await requestLiveStatus('Nika_LiveTV');
  const initialFailureHtml = elements.liveStreamsList.innerHTML;
  const initialFailureState = liveStreamStatuses.get('nika_livetv');

  liveStatusLastUpdatedAt = new Date('2026-08-23T20:00:00Z');
  liveStreamStatuses = new Map([[
    'nika_livetv', {state:'live', streamer:'nika_livetv', title:'Last confirmed title'}
  ]]);
  apiHandler = async () => { throw new Error('synthetic manual failure'); };
  await requestLiveStatus('Nika_LiveTV');
  const manualFailureHtml = elements.liveStreamsList.innerHTML;
  const manualFailureState = liveStreamStatuses.get('nika_livetv');

  liveRecordingJobs = [{
    id:'recording-visible', type:'recording', streamer:'nika_livetv', state:'running',
    recorded_seconds:12, title:'Still recording'
  }];
  const recordingVisibility = {};
  for (const state of ['unknown', 'checking', 'error']) {
    liveStreamStatuses.set('nika_livetv', {state, streamer:'nika_livetv'});
    renderLiveStreams();
    recordingVisibility[state] = elements.liveStreamsList.innerHTML;
  }

  calls = [];
  liveRecordingJobs = [];
  liveStreamers = ['One', 'Two', 'Three', 'Four', 'Five'];
  liveStreamStatuses = new Map(liveStreamers.map(streamer => [
    canonicalStreamerLoginClient(streamer),
    {state:'unknown', streamer:canonicalStreamerLoginClient(streamer)}
  ]));
  liveStatusLastUpdatedAt = null;
  let activeRequests = 0;
  let maximumActiveRequests = 0;
  apiHandler = async path => {
    activeRequests += 1;
    maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
    await new Promise(resolve => setTimeout(resolve, 2));
    activeRequests -= 1;
    return {streamer:path.split('streamer=')[1], state:'offline'};
  };
  await refreshLiveStatuses();

  calls = [];
  liveStreamStatuses = new Map();
  apiHandler = path => new Promise(resolve => setTimeout(() => resolve({
    streamer:'nika_livetv', state:'live', title:'Deduplicated'
  }), 5));
  await Promise.all([
    requestLiveStatus('Nika_LiveTV'),
    requestLiveStatus('@nika_livetv')
  ]);
  const duplicateCalls = calls.filter(call => call.path.startsWith('/api/live/status'));

  calls = [];
  pollMode = 'resolved';
  pollCalls = 0;
  liveRecordingJobs = [];
  liveRecordingActions = new Map();
  liveStreamStatuses.set('nika_livetv', {state:'live', streamer:'nika_livetv'});
  apiHandler = async () => ({job_id:'recording-9', state:'queued'});
  await startLiveRecording('@Nika_LiveTV');
  const startCall = calls.find(call => call.path === '/api/live/record');

  calls = [];
  pollMode = 'never';
  let stopResolved = false;
  apiHandler = async () => ({job_id:'recording-7', state:'stopping'});
  await stopLiveRecording('recording-7', 'Nika_LiveTV');
  stopResolved = true;
  const stopCall = calls.find(call => call.path.includes('/stop'));

  process.stdout.write(JSON.stringify({
    configuredHtml, configuredSummary, partialInitialHtml, partialInitialSummary,
    liveHtml, offlineHtml, errorHtml, statuses,
    duration:formatRecordingDuration(5077),
    invalidDuration:formatRecordingDuration('invalid'),
    hierarchyHtml, summaryText, collapsedHtml, expandedHtml, recollapsedHtml,
    stoppedSavedHtml, naturalSavedHtml, failedRecordingHtml,
    recordingOverridesErrorHtml,
    otherStreamerHtml, noLiveHtml,
    autoStartedHtml, autoPausedLiveHtml, autoEnabledLiveHtml,
    autoDegradedLiveHtml, autoUnavailableLiveHtml, autoRecorderViews,
    refreshPaths:refreshCalls.map(call => call.path),
    duplicateCount:duplicateCalls.length,
    updatingMessage, updatingDisabled, refreshedDisabled,
    initialRefreshHtml, oneResultMessage, oneResultHtml,
    updatedBeforeFirstResult:updatedBeforeFirstResult === null,
    updatedAfterFirstResult:updatedAfterFirstResult === null,
    updatedAfterRefresh:updatedAfterRefresh instanceof Date,
    manualPendingHtml, manualPendingSummary,
    manualAfterFirstHtml, manualAfterFirstSummary,
    manualFinalHtml, manualFinalSummary,
    initialFailureHtml, initialFailureState,
    manualFailureHtml, manualFailureState,
    recordingVisibility, maximumActiveRequests,
    refreshMessage:elements.liveStreamsRefreshStatus.textContent,
    startPath:startCall.path,
    startBody:JSON.parse(startCall.options.body),
    stopPath:stopCall.path,
    stopMethod:stopCall.options.method,
    stopResolved,
    conflict:friendlyRecordingActionError(new Error('recording_conflict'), 'start')
  }));
})().catch(error => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
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


class _IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids.extend(value for key, value in attrs if key == "id" and value)


class V11UiContractTests(unittest.TestCase):
    def test_live_streams_section_uses_configured_streamers_and_safe_states(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertIn('id="liveStreamsSection"', TEMPLATE)
        self.assertIn('id="refreshLiveStatuses"', TEMPLATE)
        self.assertIn("Checking live status…", result["configuredHtml"])
        self.assertNotIn("Nika_LiveTV", result["configuredHtml"])
        self.assertNotIn("DigitalGirlUli", result["configuredHtml"])
        self.assertNotIn("Offline Streamers", result["configuredHtml"])
        self.assertEqual(result["configuredSummary"], "0 Live · 0 Recording · 0 Offline")
        self.assertIn("Synthetic stream title", result["liveHtml"])
        self.assertIn("LIVE", result["liveHtml"])
        self.assertIn("Start Recording", result["liveHtml"])
        self.assertIn("Offline", result["offlineHtml"])
        self.assertNotIn("Start Recording", result["offlineHtml"])
        self.assertIn("Status could not be loaded", result["errorHtml"])
        self.assertNotIn("yt-dlp", result["errorHtml"])

    def test_initial_live_status_only_renders_confirmed_results(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertIn("First confirmed live", result["partialInitialHtml"])
        self.assertIn("Nika_LiveTV", result["partialInitialHtml"])
        self.assertIn("Offline Streamers · 1", result["partialInitialHtml"])
        self.assertIn("DigitalGirlUli", result["partialInitialHtml"])
        self.assertNotIn("Bearykchen", result["partialInitialHtml"])
        self.assertNotIn("Xerax_TTV", result["partialInitialHtml"])
        self.assertEqual(
            result["partialInitialSummary"], "1 Live · 0 Recording · 1 Offline"
        )

    def test_live_summary_priority_grid_and_empty_state(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertEqual(result["summaryText"], "2 Live · 1 Recording · 2 Offline")
        self.assertIn('class="live-stream-grid"', result["hierarchyHtml"])
        self.assertLess(
            result["hierarchyHtml"].index('data-live-streamer="nika_livetv"'),
            result["hierarchyHtml"].index("Offline Streamers · 2"),
        )
        self.assertIn("LIVE · RECORDING", result["hierarchyHtml"])
        self.assertIn("Recording 01:24:37", result["hierarchyHtml"])
        self.assertIn("Stop Recording", result["hierarchyHtml"])
        self.assertIn("No configured streamer is currently live.", result["noLiveHtml"])
        self.assertIn("Offline Streamers · 2", result["noLiveHtml"])

    def test_live_status_refresh_is_bounded_deduplicated_and_manual(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertEqual(
            sorted(result["refreshPaths"]),
            [
                "/api/live/status?streamer=digitalgirluli",
                "/api/live/status?streamer=nika_livetv",
            ],
        )
        self.assertEqual(result["duplicateCount"], 1)
        self.assertEqual(result["updatingMessage"], "Updating 0 / 2")
        self.assertEqual(result["oneResultMessage"], "Updating 1 / 2")
        self.assertTrue(result["updatingDisabled"])
        self.assertFalse(result["refreshedDisabled"])
        self.assertRegex(result["refreshMessage"], r"^Updated \d{2}:\d{2}$")
        self.assertTrue(result["updatedBeforeFirstResult"])
        self.assertTrue(result["updatedAfterFirstResult"])
        self.assertTrue(result["updatedAfterRefresh"])
        self.assertEqual(result["maximumActiveRequests"], 2)
        self.assertIn("const LIVE_STATUS_CONCURRENCY = 2", JAVASCRIPT)
        self.assertEqual(JAVASCRIPT.count("setInterval(() => pollJobs()"), 1)
        self.assertNotIn("setInterval(() => refreshLiveStatuses", JAVASCRIPT)

    def test_initial_refresh_empty_state_changes_only_after_all_results(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertIn("Checking live status…", result["initialRefreshHtml"])
        self.assertNotIn(
            "No configured streamer is currently live.", result["initialRefreshHtml"]
        )
        self.assertIn('data-live-streamer="nika_livetv"', result["oneResultHtml"])
        self.assertIn("Offline Streamers · 2", result["noLiveHtml"])
        self.assertIn("No configured streamer is currently live.", result["noLiveHtml"])

    def test_manual_refresh_keeps_confirmed_status_until_each_result(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertEqual(
            result["manualPendingSummary"], "1 Live · 0 Recording · 1 Offline"
        )
        self.assertIn("Previously live", result["manualPendingHtml"])
        self.assertIn("Offline Streamers · 1", result["manualPendingHtml"])
        self.assertIn("DigitalGirlUli", result["manualPendingHtml"])
        self.assertIn("Updating live status…", result["manualPendingHtml"])
        self.assertEqual(
            result["manualAfterFirstSummary"], "0 Live · 0 Recording · 2 Offline"
        )
        self.assertNotIn("Previously live", result["manualAfterFirstHtml"])
        self.assertEqual(
            result["manualFinalSummary"], "1 Live · 0 Recording · 1 Offline"
        )
        self.assertIn("Newly live", result["manualFinalHtml"])

    def test_live_status_failures_never_become_offline(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertEqual(result["initialFailureState"]["state"], "error")
        self.assertIn("Status could not be loaded", result["initialFailureHtml"])
        self.assertNotIn("Offline Streamers", result["initialFailureHtml"])
        self.assertEqual(result["manualFailureState"]["state"], "live")
        self.assertTrue(result["manualFailureState"]["refreshError"])
        self.assertIn("Last confirmed title", result["manualFailureHtml"])
        self.assertIn(
            "Status refresh failed; showing the last confirmed status.",
            result["manualFailureHtml"],
        )
        self.assertNotIn("Offline Streamers", result["manualFailureHtml"])

    def test_active_recording_remains_visible_for_unconfirmed_live_states(self) -> None:
        result = _evaluate_live_stream_ui()

        for state in ("unknown", "checking", "error"):
            with self.subTest(state=state):
                html = result["recordingVisibility"][state]
                self.assertIn("LIVE · RECORDING", html)
                self.assertIn("Still recording", html)
                self.assertIn("Stop Recording", html)

    def test_recording_jobs_drive_lifecycle_duration_and_failure_copy(self) -> None:
        result = _evaluate_live_stream_ui()
        statuses = result["statuses"]

        self.assertEqual(result["duration"], "01:24:37")
        self.assertEqual(result["invalidDuration"], "00:00:00")
        self.assertEqual(statuses["queued"], "Recording is starting…")
        self.assertEqual(statuses["running"], "Recording 01:24:37")
        self.assertEqual(statuses["stopping"], "Recording is stopping…")
        self.assertEqual(statuses["natural"], "Stream ended · recording saved")
        self.assertEqual(statuses["stopped"], "Recording saved")
        self.assertEqual(statuses["processError"], "Recording failed")
        self.assertEqual(statuses["incomplete"], "Recording could not be saved completely")
        self.assertEqual(statuses["stopFailed"], "Recording could not be stopped cleanly")
        self.assertIn("LIVE · RECORDING", result["recordingOverridesErrorHtml"])
        self.assertIn("Recording 01:24:37", result["recordingOverridesErrorHtml"])
        self.assertIn('data-job-id="recording-7"', result["recordingOverridesErrorHtml"])
        self.assertEqual(result["stoppedSavedHtml"].count("Recording saved"), 1)
        self.assertEqual(
            result["naturalSavedHtml"].count("Stream ended · recording saved"), 1
        )
        self.assertIn('class="live-stream-card is-error"', result["failedRecordingHtml"])
        self.assertIn(
            "Recording could not be saved completely", result["failedRecordingHtml"]
        )
        self.assertIn("if (job?.type === 'recording') {", JAVASCRIPT)

        queue_items = _classify_download_jobs(
            [
                {
                    "id": "recording-7",
                    "type": "recording",
                    "label": "Live recording: nika_livetv",
                    "streamer": "nika_livetv",
                    "state": "running",
                    "urls": ["nika_livetv"],
                    "item_ids": ["recording-7-item-1"],
                    "item_states": ["running"],
                    "log": [],
                }
            ]
        )
        self.assertEqual(queue_items, [])

    def test_auto_recorder_status_states_are_truthful_and_compact(self) -> None:
        result = _evaluate_live_stream_ui()["autoRecorderViews"]

        self.assertEqual(result["loading"]["title"], "Auto Recorder · Checking…")
        self.assertEqual(result["running"]["title"], "Auto Recorder · Running")
        self.assertIn("Watching 4", result["running"]["detail"])
        self.assertIn("Last checked", result["running"]["detail"])
        self.assertEqual(result["paused"]["title"], "Auto Recorder · Paused")
        self.assertEqual(result["paused"]["detail"], "4 streamers selected")
        self.assertEqual(result["zero"]["kind"], "running")
        self.assertEqual(result["zero"]["detail"], "No streamers selected")
        self.assertEqual(result["degraded"]["kind"], "degraded")
        self.assertIn("State file invalid", result["degraded"]["detail"])
        self.assertIn("paused for safety", result["degraded"]["detail"])
        self.assertEqual(
            result["failed"]["title"], "Auto Recorder status unavailable"
        )
        self.assertNotIn("Paused", result["failed"]["title"])
        self.assertEqual(result["native"]["title"], "Auto Recorder · Unavailable")

    def test_live_cards_explain_auto_recording_without_competing_with_live_state(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertIn("LIVE · RECORDING", result["autoStartedHtml"])
        self.assertIn("Started automatically", result["autoStartedHtml"])
        self.assertIn("Auto Recording enabled", result["autoStartedHtml"])
        self.assertIn(
            "Auto Recording selected · Auto Recorder paused",
            result["autoPausedLiveHtml"],
        )
        self.assertIn("Auto Recording enabled", result["autoEnabledLiveHtml"])
        self.assertNotIn("Started automatically", result["autoEnabledLiveHtml"])
        self.assertIn(
            "Auto Recording selected · Auto Recorder degraded",
            result["autoDegradedLiveHtml"],
        )
        self.assertIn(
            "Auto Recording selected · Auto Recorder unavailable",
            result["autoUnavailableLiveHtml"],
        )

    def test_auto_recorder_polling_is_modest_and_separate_from_live_lookups(self) -> None:
        self.assertIn("const AUTO_RECORDER_STATUS_REFRESH_MS = 15000", JAVASCRIPT)
        self.assertEqual(
            JAVASCRIPT.count(
                "setInterval(() => refreshAutoRecorderStatus().catch(() => {}), AUTO_RECORDER_STATUS_REFRESH_MS)"
            ),
            1,
        )
        status_body = JAVASCRIPT.split(
            "async function refreshAutoRecorderStatus()", 1
        )[1].split("function formatRecordingDuration", 1)[0]
        self.assertIn("/api/auto-recorder/status", status_body)
        self.assertNotIn("/api/live/status", status_body)

    def test_recording_actions_are_minimal_fast_and_globally_exclusive(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertEqual(result["startPath"], "/api/live/record")
        self.assertEqual(result["startBody"], {"streamer": "nika_livetv"})
        self.assertEqual(result["stopPath"], "/api/live/record/recording-7/stop")
        self.assertEqual(result["stopMethod"], "POST")
        self.assertTrue(result["stopResolved"])
        self.assertEqual(result["conflict"], "Another recording is already active.")
        self.assertIn("Start Recording", result["otherStreamerHtml"])
        self.assertIn("disabled", result["otherStreamerHtml"])
        self.assertIn("Another recording is already active.", result["otherStreamerHtml"])
        self.assertIn("pollJobs().catch(() => {})", JAVASCRIPT)

    def test_live_streams_mobile_and_accessibility_contract(self) -> None:
        self.assertIn('role="status" aria-live="polite"', TEMPLATE)
        self.assertIn('id="liveStreamsList" class="live-stream-list" aria-live="polite"', TEMPLATE)
        self.assertIn('id="liveStreamsSummary" class="live-stream-summary" aria-live="polite"', TEMPLATE)
        self.assertIn('class="offline-streams-toggle" aria-expanded=', JAVASCRIPT)
        self.assertIn('aria-controls="offlineStreamersList"', JAVASCRIPT)
        self.assertIn(".live-stream-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));", STYLESHEET)
        self.assertIn(".live-stream-grid, .live-stream-grid.is-single { grid-template-columns:1fr; }", STYLESHEET)
        self.assertIn(".live-stream-actions button { width:100%; min-height:44px; }", STYLESHEET)
        self.assertIn(".offline-stream-grid { grid-template-columns:1fr; }", STYLESHEET)
        self.assertIn(".live-heading-statuses { justify-items:stretch;", STYLESHEET)
        self.assertIn(".streamer-auto-record-field { grid-column:2; }", STYLESHEET)
        self.assertIn("html, body { max-width:100%; overflow-x:hidden; }", STYLESHEET)

    def test_offline_disclosure_is_compact_collapsed_and_toggleable(self) -> None:
        result = _evaluate_live_stream_ui()

        self.assertIn('aria-expanded="false"', result["collapsedHtml"])
        self.assertIn('aria-label="Show 2 offline streamers"', result["collapsedHtml"])
        self.assertIn('class="offline-stream-grid" hidden', result["collapsedHtml"])
        self.assertIn('aria-expanded="true"', result["expandedHtml"])
        self.assertIn('aria-label="Hide 2 offline streamers"', result["expandedHtml"])
        self.assertIn('class="offline-stream-grid"', result["expandedHtml"])
        self.assertNotIn('class="offline-stream-grid" hidden', result["expandedHtml"])
        self.assertIn('aria-expanded="false"', result["recollapsedHtml"])
        self.assertIn('class="offline-stream-item"', result["expandedHtml"])
        self.assertNotIn("Start Recording", result["expandedHtml"].split("Offline Streamers", 1)[1])
        self.assertIn(".offline-stream-grid[hidden] { display:none; }", STYLESHEET)

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
        self.assertNotIn("Ready for another VOD?", TEMPLATE)
        self.assertNotIn('id="dashboardIdle"', TEMPLATE)

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
        for heading in ("Running", "Up Next", "Needs Attention"):
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

    def test_queue_controls_are_backend_capability_driven_without_reorder(self) -> None:
        self.assertIn('id="queueLaneControls"', TEMPLATE)
        self.assertIn("job.item_capabilities", JAVASCRIPT)
        for label in (
            "Pause Queue",
            "Resume Queue",
            "Stop after current",
            "Remove from Queue",
            "Retry",
            "Cancel",
        ):
            self.assertIn(label, JAVASCRIPT)
        self.assertNotIn("Move Up", JAVASCRIPT)
        self.assertNotIn("Move Down", JAVASCRIPT)
        self.assertIn("Active work continues; no new item will start.", JAVASCRIPT)

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

    def test_baseline_search_status_keeps_manual_download_available(self) -> None:
        self.assertEqual(
            _search_result_status({"auto_vod_baseline_existing": True}),
            'Baseline<br><span class="muted">Manual download available</span>',
        )
        self.assertEqual(
            _search_result_status(
                {"auto_vod_baseline_existing": True, "already_downloaded": True}
            ),
            "Already in Archive",
        )
        self.assertIn('class="rowcheck" type="checkbox"', JAVASCRIPT)
        self.assertIn("data-url=\"${escapeHtml(r.url)}\"", JAVASCRIPT)

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

    def test_removed_uploaded_file_has_truthful_state_and_no_actions(self) -> None:
        removed = {
            "path": "C:/media/Example/removed.mp4",
            "name": "removed.mp4",
            "streamer": "Example",
            "date_de": "18.08.2026",
            "title": "Removed upload",
            "size_gb": None,
            "prepared": False,
            "dashboard_uploaded": True,
            "manually_uploaded": False,
            "already_uploaded": True,
            "local_file_exists": False,
            "status": "Local file removed",
        }

        result = _evaluate_local_history_ui(removed, [removed])
        html = result["card"]

        self.assertIn("Uploaded to YouTube", html)
        self.assertIn("Local file removed", html)
        self.assertIn("Upload history retained", html)
        self.assertIn("is-local-removed", html)
        self.assertIn(">History<", html)
        self.assertNotIn("localvideocheck", html)
        self.assertNotIn("data-action=", html)
        self.assertNotIn(">Ready<", html)

    def test_uploaded_history_initially_limits_rendering_without_deleting_data(self) -> None:
        pending = [
            {"path": f"pending-{index}", "already_uploaded": False}
            for index in range(2)
        ]
        uploaded = [
            {"path": f"uploaded-{index}", "already_uploaded": True}
            for index in range(35)
        ]

        result = _evaluate_local_history_ui(
            {
                "path": "removed",
                "name": "removed.mp4",
                "already_uploaded": True,
                "dashboard_uploaded": True,
                "local_file_exists": False,
                "status": "Local file removed",
            },
            pending + uploaded,
        )

        self.assertEqual(result["sourceCount"], 37)
        self.assertEqual(len(result["visiblePaths"]), 22)
        self.assertEqual(result["visiblePaths"][:2], ["pending-0", "pending-1"])
        self.assertEqual(result["visiblePaths"][-1], "uploaded-19")
        self.assertIn("Show more", JAVASCRIPT)
        self.assertIn("grid-column:1 / -1", STYLESHEET)

    def test_streamer_editor_preserves_text_storage_contract(self) -> None:
        self.assertIn('id="streamerAddInput"', TEMPLATE)
        self.assertIn('id="streamerEditorList"', TEMPLATE)
        self.assertIn('id="streamersText" class="hidden"', TEMPLATE)
        self.assertIn('id="streamerPlaylistStatus"', TEMPLATE)
        self.assertIn("setStreamerEditorNames", JAVASCRIPT)
        self.assertIn('class="streamer-playlist-select"', JAVASCRIPT)
        self.assertIn("streamer_profiles:streamerProfileDraft", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"remove\"", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"up\"", JAVASCRIPT)
        self.assertIn("data-streamer-action=\"down\"", JAVASCRIPT)

    def test_streamer_playlist_ui_represents_sets_and_removes_overrides(self) -> None:
        result = _evaluate_playlist_ui()

        self.assertEqual(result["configured"], "PLAYLIST_A")
        self.assertEqual(result["inherited"], "")
        self.assertTrue(result["autoConfigured"])
        self.assertFalse(result["autoMissing"])
        self.assertTrue(result["autoYoutubeConfigured"])
        self.assertFalse(result["autoYoutubeMissing"])
        self.assertIn('value="PLAYLIST_A" selected', result["configuredOptions"])
        self.assertIn(">Global Default</option>", result["inheritedOptions"])
        self.assertEqual(
            result["clonedProfiles"]["digitalgirluli"],
            {
                "youtube_playlist_id": "PLAYLIST_A",
                "auto_record": True,
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        )
        self.assertEqual(
            result["clonedProfiles"]["auto_only"],
            {"auto_record": True},
        )
        self.assertNotIn("false_only", result["clonedProfiles"])
        self.assertNotIn("string_true", result["clonedProfiles"])
        self.assertEqual(
            result["clonedProfiles"]["auto_vod_only"],
            {"auto_vod_download": True},
        )
        self.assertEqual(
            result["clonedProfiles"]["auto_youtube_only"],
            {"auto_youtube_upload": True},
        )
        self.assertEqual(
            result["setOverride"]["digitalgirluli"],
            {
                "youtube_playlist_id": "PLAYLIST_B",
                "auto_record": True,
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        )
        self.assertEqual(
            result["setOverride"]["orphan_streamer"],
            {"youtube_playlist_id": "ORPHAN"},
        )
        self.assertEqual(
            result["removedOverride"]["digitalgirluli"],
            {
                "auto_record": True,
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        )
        self.assertEqual(
            result["removedOverride"]["orphan_streamer"],
            {"youtube_playlist_id": "ORPHAN"},
        )

    def test_auto_recorder_controls_are_visible_compact_and_accessible(self) -> None:
        self.assertIn('id="autoRecorderEnabled"', TEMPLATE)
        self.assertNotIn('id="autoRecorderEnabled" checked', TEMPLATE)
        self.assertIn('role="switch"', TEMPLATE)
        self.assertIn('aria-label="Enable Auto Recorder"', TEMPLATE)
        self.assertIn(
            "$('autoRecorderEnabled').checked = state.settings.auto_recorder_enabled === true",
            JAVASCRIPT,
        )
        self.assertIn(
            "auto_recorder_enabled:$('autoRecorderEnabled').checked",
            JAVASCRIPT,
        )
        self.assertIn("Paused · streamer selections are preserved.", TEMPLATE)
        self.assertIn('class="streamer-auto-record-toggle"', JAVASCRIPT)
        self.assertIn(
            'aria-label="Enable automatic recording for ${escapeHtml(name)}"',
            JAVASCRIPT,
        )
        self.assertIn("min-height:44px", STYLESHEET)
        self.assertIn("input:focus-visible + .switch-track", STYLESHEET)

    def test_auto_youtube_settings_are_visible_but_not_an_active_workflow(self) -> None:
        self.assertIn('id="autoYoutubeEnabled"', TEMPLATE)
        self.assertNotIn('id="autoYoutubeEnabled" checked', TEMPLATE)
        self.assertIn("Auto YouTube settings can be saved now. Automation is not active yet.", TEMPLATE)
        self.assertIn(
            "$('autoYoutubeEnabled').checked = state.settings.auto_youtube_enabled === true",
            JAVASCRIPT,
        )
        self.assertIn(
            "auto_youtube_enabled:$('autoYoutubeEnabled').checked",
            JAVASCRIPT,
        )
        self.assertIn('class="streamer-auto-youtube-toggle"', JAVASCRIPT)
        self.assertIn(
            'aria-label="Enable automatic YouTube uploads for ${escapeHtml(name)}"',
            JAVASCRIPT,
        )
        self.assertNotIn('data-page="auto-youtube"', TEMPLATE)
        self.assertIn(".streamer-editor-row", STYLESHEET)
        self.assertIn("grid-template-columns:34px minmax(0,1fr)", STYLESHEET)

    def test_streamer_auto_record_and_playlist_round_trip_independently(self) -> None:
        result = _evaluate_playlist_ui()
        self.assertEqual(
            result["autoEnabled"]["digitalgirluli"],
            {"youtube_playlist_id": "PLAYLIST_A", "auto_record": True},
        )
        self.assertEqual(
            result["playlistChangedAfterAuto"]["digitalgirluli"],
            {"youtube_playlist_id": "PLAYLIST_B", "auto_record": True},
        )
        self.assertEqual(
            result["autoRemovedAfterPlaylist"]["digitalgirluli"],
            {"youtube_playlist_id": "PLAYLIST_B"},
        )
        self.assertEqual(result["emptyProfileRemoved"], {})
        self.assertEqual(
            result["autoEnabledAfterVod"]["digitalgirluli"],
            {
                "youtube_playlist_id": "PLAYLIST_A",
                "auto_record": True,
                "auto_vod_download": True,
            },
        )
        self.assertEqual(
            result["autoRemovedAfterVod"]["digitalgirluli"],
            {"youtube_playlist_id": "PLAYLIST_A", "auto_vod_download": True},
        )

    def test_streamer_auto_youtube_and_playlist_round_trip_independently(self) -> None:
        result = _evaluate_playlist_ui()
        self.assertEqual(
            result["autoYoutubeEnabled"]["digitalgirluli"],
            {
                "youtube_playlist_id": "PLAYLIST_A",
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        )
        self.assertEqual(
            result["autoYoutubeRemoved"]["digitalgirluli"],
            {"youtube_playlist_id": "PLAYLIST_A", "auto_vod_download": True},
        )
        self.assertEqual(result["emptyAutoYoutubeProfileRemoved"], {})

    def test_playlist_refresh_failure_preserves_streamer_profile_draft(self) -> None:
        loader = JAVASCRIPT.split(
            "async function loadYoutubePlaylists()", 1
        )[1].split("function friendlyYoutubeConnectError", 1)[0]
        connection_status = JAVASCRIPT.split(
            "async function refreshYoutubeStatus()", 1
        )[1].split("async function loadYoutubePlaylists()", 1)[0]
        self.assertIn(
            "Existing streamer defaults are preserved.", loader
        )
        self.assertNotIn("streamerProfileDraft =", loader)
        self.assertIn(
            "youtubePlaylistChoices = Array.isArray(data.playlists)", loader
        )
        self.assertNotIn("streamer-playlist-select", connection_status)

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

    def test_local_upload_playlist_request_semantics_match_for_each_job_size(self) -> None:
        result = _evaluate_playlist_ui()

        self.assertEqual(result["defaultSingle"], {"paths": ["one.mp4"]})
        self.assertEqual(
            result["defaultMultiple"],
            {"paths": ["one.mp4", "two.mp4"]},
        )
        self.assertEqual(
            result["noPlaylistSingle"],
            {"paths": ["one.mp4"], "playlist_id": ""},
        )
        self.assertEqual(
            result["noPlaylistMultiple"],
            {"paths": ["one.mp4", "two.mp4"], "playlist_id": ""},
        )
        self.assertEqual(
            result["explicitSingle"],
            {"paths": ["one.mp4"], "playlist_id": "PLAYLIST_A"},
        )
        self.assertEqual(
            result["explicitMultiple"],
            {
                "paths": ["one.mp4", "two.mp4"],
                "playlist_id": "PLAYLIST_A",
            },
        )
        self.assertIn('id="localUploadPlaylistId"', TEMPLATE)
        self.assertIn(
            "JSON.stringify(localUploadRequestPayload([path]))", JAVASCRIPT
        )
        self.assertIn(
            "JSON.stringify(localUploadRequestPayload(paths))", JAVASCRIPT
        )

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
        self.assertIn(".queue-lane-controls { grid-template-columns:1fr; }", STYLESHEET)
        self.assertIn(".queue-item-actions button { width:100%; min-height:44px; }", STYLESHEET)


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

        self.assertIn('data-queue-detail-id="youtube_upload:upload-1:upload-1-item-1" open', html)
        self.assertIn("Mark as resolved", html)

    def test_completed_upload_details_exclude_another_active_vod(self) -> None:
        job = _upload_job(["fertig", "l\u00e4uft"], [100, 12])
        job["item_metadata"][0].update(
            {"streamer": "XERAX_TTV", "title": "( Peak ) G? was nun"}
        )
        job["item_metadata"][1].update(
            {
                "streamer": "XERAX_TTV",
                "title": "( Peak ) Neuer Tag neues Gl\u00fcck",
            }
        )
        job["log"] = [
            "Uploading local VOD file: C:/media/vod-1.mp4",
            "YouTube Upload starting: vod-1.mp4 (private)",
            "YouTube Upload vod-1.mp4: 100%",
            "YouTube Upload completed: https://www.youtube.com/watch?v=old",
            "Uploading local VOD file: C:/media/vod-2.mp4",
            "YouTube Upload starting: vod-2.mp4 (private)",
            "YouTube Upload vod-2.mp4: 12%",
        ]

        completed, active = _classify_download_jobs([job])

        self.assertIn("watch?v=old", "\n".join(completed["detailLogs"]))
        self.assertNotIn("vod-2.mp4", "\n".join(completed["detailLogs"]))
        self.assertIn("vod-2.mp4: 12%", "\n".join(active["detailLogs"]))

        html = _render_queue_item_with_saved_open_state(
            {
                "job": {
                    "id": "upload-1",
                    "type": "youtube_upload",
                    "label": "Upload",
                    "log": job["log"],
                },
                "index": 0,
                "state": "completed",
                "operation": "YouTube upload completed",
                "streamer": "XERAX_TTV",
                "date": "18.08.2026",
                "title": "( Peak ) G? was nun",
                "detailLogs": completed["detailLogs"],
                "error": "",
                "resolved": False,
                "progress": None,
                "extra": "",
            }
        )
        self.assertIn("watch?v=old", html)
        self.assertNotIn("vod-2.mp4: 12%", html)
        self.assertIn('data-queue-detail-id="youtube_upload:upload-1:upload-1-item-1" open', html)

    def test_same_streamer_vods_have_independent_polling_stable_keys(self) -> None:
        job = _upload_job(["fertig", "l\u00e4uft"], [100, 12])
        for metadata in job["item_metadata"]:
            metadata.update(
                {
                    "streamer": "XERAX_TTV",
                    "date": "18.08.2026",
                    "title": "Overlapping metadata",
                }
            )
        first_poll = _classify_download_jobs([job])
        job["log"].append("YouTube Upload vod-2.mp4: 13%")
        second_poll = _classify_download_jobs([job])

        self.assertEqual(
            [item["key"] for item in first_poll],
            [
                "youtube_upload:upload-1:upload-1-item-1",
                "youtube_upload:upload-1:upload-1-item-2",
            ],
        )
        self.assertEqual(
            [item["key"] for item in second_poll],
            [item["key"] for item in first_poll],
        )
        self.assertEqual(len(set(item["key"] for item in second_poll)), 2)

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
