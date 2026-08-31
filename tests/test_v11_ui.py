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
process.stdout.write(JSON.stringify({
  clonedProfiles,
  configuredOptions: playlistOptionsHtml('PLAYLIST_A', 'No playlist'),
  emptyOptions: playlistOptionsHtml('', 'No playlist'),
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


def _evaluate_streamer_workspace_filters() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for streamer filter tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function canonicalStreamerLoginClient');
const end = source.indexOf('function setStreamerEditorNames', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Streamer filter helpers not found');
eval(source.slice(start, end));
const names = ['Manual', 'AutoDownload', 'AutoYoutube', 'LiveBravo', 'ReviewCase'];
const product = {streamer_policies:{
  manual:{vod_handling:'manual', live_recording:'manual', validation:{state:'valid'}},
  autodownload:{vod_handling:'auto_download', live_recording:'manual', validation:{state:'valid'}},
  autoyoutube:{vod_handling:'download_and_youtube', live_recording:'manual', validation:{state:'valid'}},
  livebravo:{vod_handling:'manual', live_recording:'automatic', validation:{state:'valid'}},
  reviewcase:{vod_handling:'needs_review', live_recording:'manual', validation:{state:'needs_review'}}
}};
const before = JSON.stringify({names, product});
const listed = (query, filter) => streamerWorkspaceEntries(names, product, query, filter).map(entry => entry.name);
process.stdout.write(JSON.stringify({
  all:listed('', 'all'),
  automated:listed('', 'automated'),
  live:listed('', 'live-recording'),
  review:listed('', 'needs-review'),
  caseInsensitive:listed('aUtO', 'all'),
  combined:listed('youtube', 'automated'),
  none:listed('missing', 'all'),
  after:JSON.stringify({names, product}),
  before
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


def _evaluate_streamer_avatar_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for avatar UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const loaderStart = source.indexOf('function normalizeStreamerProfileMap');
const loaderEnd = source.indexOf('function localCalendarDate');
const avatarStart = source.indexOf('function streamerProfileFor');
const avatarEnd = source.indexOf('function automationProductView');
if ([loaderStart, loaderEnd, avatarStart, avatarEnd].some(index => index < 0)) throw new Error('Avatar helpers not found');
let streamerProfilesByLogin = new Map();
let streamerProfilesLoadPromise = null;
let streamerProfilesLoaded = false;
let calls = 0;
let shouldFail = false;
function canonicalStreamerLoginClient(value) {
  const login = String(value || '').trim().replace(/^@+/, '').toLowerCase();
  return /^[a-z0-9_]{1,25}$/.test(login) ? login : '';
}
function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, character => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  })[character]);
}
function api(path) {
  calls += 1;
  if (shouldFail) return Promise.reject(new Error('profiles unavailable'));
  if (path !== '/api/streamer-profiles') throw new Error('unexpected path');
  return Promise.resolve({profiles:{
    NIKA_LIVETV:{login:'Nika_LiveTV', display_name:'Nika LiveTV', avatar_url:'/api/streamer-avatar/nika_livetv'},
    no_image:{login:'No_Image', display_name:'No Image'}
  }});
}
eval(source.slice(loaderStart, loaderEnd));
eval(source.slice(avatarStart, avatarEnd));
(async () => {
  const first = loadStreamerProfiles();
  const second = loadStreamerProfiles();
  await Promise.all([first, second]);
  const imageHtml = streamerAvatarHtml('NIKA_LIVETV', 'compact');
  const missingHtml = streamerAvatarHtml('digitalgirluli');
  const noImageHtml = streamerAvatarHtml('no_image');
  let errorHandler = null;
  let removed = false;
  const image = { addEventListener:(name, handler) => { if (name === 'error') errorHandler = handler; }, remove:() => { removed = true; } };
  wireStreamerAvatarFallbacks({querySelectorAll:() => [image]});
  errorHandler();
  const callsAfterRender = calls;
  streamerProfilesByLogin = new Map();
  streamerProfilesLoadPromise = null;
  streamerProfilesLoaded = false;
  shouldFail = true;
  await loadStreamerProfiles();
  const failureHtml = streamerAvatarHtml('nika_livetv');
  process.stdout.write(JSON.stringify({
    callsAfterLoad:callsAfterRender,
    imageHtml, missingHtml, noImageHtml, failureHtml, removed,
    profileKeys:[...streamerProfilesByLogin.keys()]
  }));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
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


def _evaluate_toast_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for toast UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('const TOAST_VARIANTS');
const end = source.indexOf('async function copyTextToClipboard', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Toast helpers not found');
let timerId = 0;
const timers = new Map();
function setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; }
function clearTimeout(id) { timers.delete(id); }
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.className = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); children.forEach(child => child.parent = this); }
  appendChild(child) { this.append(child); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll(selector) { return selector === '.app-toast' ? this.children.filter(child => child.className.includes('app-toast')) : []; }
}
const container = new Element('div');
const document = {getElementById:id => id === 'appToastContainer' ? container : null, createElement:tag => new Element(tag)};
eval(source.slice(start, end));
const success = showToast('<b>safe text</b>', {variant:'success'});
const warning = showToast('Check this', {variant:'warning', duration:10});
const error = showToast('Failed', {variant:'error'});
const info = showToast('Heads up', {variant:'info'});
const beforeDismiss = container.children.length;
warning.children[1].listeners.click();
const afterDismiss = container.children.length;
const successTimer = timers.get(Number(success.dataset.toastTimer));
successTimer.callback();
process.stdout.write(JSON.stringify({
  beforeDismiss, afterDismiss, afterTimeout:container.children.length,
  success:{role:success.attributes.role, live:success.attributes['aria-live'], text:success.children[0].textContent, close:success.children[1].attributes['aria-label']},
  variants:container.children.map(toast => toast.className),
  error:{role:error.attributes.role, live:error.attributes['aria-live']}
}));
"""
    completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_confirmation_dialog_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for confirmation dialog UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('let activeConfirmation');
const end = source.indexOf('async function copyTextToClipboard', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Confirmation dialog helpers not found');
class Element {
  constructor(id) { this.id = id; this.listeners = {}; this.className = ''; this.textContent = ''; this.hidden = false; this.open = false; this.focused = false; this.classList = {toggle:(name, active) => { this.className = active ? name : ''; }}; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  removeEventListener(name, handler) { this.listeners[name] = (this.listeners[name] || []).filter(item => item !== handler); }
  dispatch(name, event={}) { (this.listeners[name] || []).slice().forEach(handler => handler({target:this, preventDefault:() => { event.prevented = true; }, ...event})); return event; }
  focus() { this.focused = true; document.activeElement = this; }
  querySelectorAll() { return [cancelButton, confirmButton]; }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch('close'); }
  setAttribute() {}
  removeAttribute() { this.open = false; }
}
const dialog = new Element('appConfirmDialog');
const title = new Element('appConfirmDialogTitle');
const message = new Element('appConfirmDialogDescription');
const cancelButton = new Element('appConfirmDialogCancel');
const confirmButton = new Element('appConfirmDialogAccept');
const trigger = new Element('trigger');
const elements = Object.fromEntries([dialog, title, message, cancelButton, confirmButton, trigger].map(item => [item.id, item]));
const document = {activeElement:trigger, getElementById:id => elements[id] || null, contains:element => element === trigger};
eval(source.slice(start, end));
(async () => {
  const confirmedPromise = confirmAction({title:'Delete local VOD', message:'<b>Unsafe</b>', confirmLabel:'Delete VOD', variant:'danger'});
  const duplicateResult = await confirmAction({title:'Second action'});
  const opened = {open:dialog.open, focused:document.activeElement.id, title:title.textContent, message:message.textContent, confirm:confirmButton.textContent, className:confirmButton.className};
  confirmButton.dispatch('click');
  const confirmed = await confirmedPromise;
  const afterConfirm = {open:dialog.open, focusReturned:document.activeElement === trigger};
  const cancelledPromise = confirmAction({title:'Cancel action'});
  cancelButton.dispatch('click');
  const cancelled = await cancelledPromise;
  const escapedPromise = confirmAction({title:'Escape action'});
  dialog.dispatch('keydown', {key:'Escape'});
  const escaped = await escapedPromise;
  const trappedPromise = confirmAction({title:'Trap focus'});
  document.activeElement = confirmButton;
  const tabEvent = dialog.dispatch('keydown', {key:'Tab', shiftKey:false});
  const tabFocus = document.activeElement.id;
  cancelButton.dispatch('click');
  await trappedPromise;
  process.stdout.write(JSON.stringify({opened, duplicateResult, confirmed, afterConfirm, cancelled, escaped, tabFocus, tabPrevented:!!tabEvent.prevented}));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
"""
    completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_button_pending_ui() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for button pending UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('const pendingButtonActions');
const end = source.indexOf('async function copyTextToClipboard', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Button pending helper not found');
class Button {
  constructor(label, disabled=false) { this.textContent = label; this.disabled = disabled; this.attributes = {}; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
}
eval(source.slice(start, end));
(async () => {
  const button = new Button('Refresh status');
  let calls = 0;
  let release;
  const action = () => { calls += 1; return new Promise(resolve => { release = resolve; }); };
  const first = withButtonPending(button, {pendingLabel:'Refreshing...'}, action);
  const duplicate = withButtonPending(button, {pendingLabel:'Refreshing...'}, action);
  const during = {label:button.textContent, disabled:button.disabled, busy:button.getAttribute('aria-busy'), samePromise:first === duplicate, calls};
  release('done');
  const result = await first;
  const afterSuccess = {result, label:button.textContent, disabled:button.disabled, busy:button.getAttribute('aria-busy'), calls};
  const failing = new Button('Check settings file');
  const failure = await withButtonPending(failing, {pendingLabel:'Checking...'}, () => Promise.reject(new Error('status unavailable'))).catch(error => error.message);
  const afterFailure = {failure, label:failing.textContent, disabled:failing.disabled, busy:failing.getAttribute('aria-busy')};
  const unavailable = new Button('Refresh Playlists', true);
  let unavailableCalls = 0;
  await withButtonPending(unavailable, {pendingLabel:'Refreshing...'}, () => { unavailableCalls += 1; });
  process.stdout.write(JSON.stringify({during, afterSuccess, afterFailure, unavailable:{label:unavailable.textContent, disabled:unavailable.disabled, calls:unavailableCalls}}));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
"""
    completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
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


def _evaluate_vod_date_presets() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for VOD date preset tests")
    runner = r"""
process.env.TZ = 'Pacific/Auckland';
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function localCalendarDate');
const end = source.indexOf('function renderState', start);
if (start < 0 || end < 0 || end <= start) throw new Error('VOD date helpers not found');
eval(source.slice(start, end));
const now = new Date(2026, 0, 1, 0, 30, 0);
process.stdout.write(JSON.stringify({
  today: dateRangeForPreset('today', now),
  yesterdayToday: dateRangeForPreset('yesterday-today', now),
  last7: dateRangeForPreset('last-7', now),
  last30: dateRangeForPreset('last-30', now)
}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner], cwd=ROOT, encoding="utf-8",
        capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_search_streamer_picker() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for streamer picker UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function storedSearchStreamerSelection');
const end = source.indexOf('function selectedUrls', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Streamer picker helpers not found');
const elements = {
  searchStreamerCheckboxes: {innerHTML:''},
  searchStreamerToggleInfo: {textContent:'', attributes:{}, setAttribute(name, value) { this.attributes[name] = value; }},
  searchStreamerFilter: {value:''}
};
function $(id) { return elements[id] || null; }
const localStore = new Map([['vodSearchStreamerSelection', JSON.stringify(['beta', 'removed_streamer'])]]);
const localStorage = {getItem: key => localStore.get(key) || null, setItem: (key, value) => localStore.set(key, value)};
const document = {querySelectorAll: selector => selector === '.search-streamer-check:checked' ? [{value:'beta'}] : []};
function escapeHtml(value) { return String(value); }
function streamerAvatarHtml(value, size) { return `<avatar data-size="${size}">${value}</avatar>`; }
function wireStreamerAvatarFallbacks() {}
let state = {streamers:['alpha', 'beta', 'alpha']};
let searchStreamerLoadState = 'ready';
let searchStreamerLoadError = '';
let searchStreamerSelection = null;
eval(source.slice(start, end));
renderSearchStreamerCheckboxes();
const loaded = {html: elements.searchStreamerCheckboxes.innerHTML, info: elements.searchStreamerToggleInfo.textContent};
elements.searchStreamerFilter.value = 'alpha';
renderSearchStreamerCheckboxes();
const filtered = {html: elements.searchStreamerCheckboxes.innerHTML, selected:selectedSearchStreamersFromCheckboxes()};
setAllSearchStreamers(false);
const cleared = {selected:selectedSearchStreamersFromCheckboxes(), stored:localStore.get('vodSearchStreamerSelection')};
setAllSearchStreamers(true);
const selectedAll = {selected:selectedSearchStreamersFromCheckboxes(), stored:localStore.get('vodSearchStreamerSelection')};
searchStreamerLoadState = 'loading';
renderSearchStreamerCheckboxes();
const loading = {html: elements.searchStreamerCheckboxes.innerHTML, info: elements.searchStreamerToggleInfo.textContent};
searchStreamerLoadState = 'error';
searchStreamerLoadError = 'Network unavailable';
renderSearchStreamerCheckboxes();
const failed = {html: elements.searchStreamerCheckboxes.innerHTML, info: elements.searchStreamerToggleInfo.textContent};
searchStreamerLoadState = 'ready';
state = {streamers:[]};
renderSearchStreamerCheckboxes();
const empty = {html: elements.searchStreamerCheckboxes.innerHTML, info: elements.searchStreamerToggleInfo.textContent};
process.stdout.write(JSON.stringify({loaded, filtered, cleared, selectedAll, loading, failed, empty}));
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


def _evaluate_search_streamer_picker_close() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for streamer picker UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function closeSearchStreamerPicker');
const end = source.indexOf('function updateVodFilterCount', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Streamer picker close helpers not found');
let focused = false;
const attributes = {};
const elements = {
  searchStreamerPickerPanel: {hidden:false},
  searchStreamerPickerToggle: {setAttribute:(name, value) => { attributes[name] = value; }, focus:() => { focused = true; }},
  searchStreamerFilter: {focus:() => { focused = 'filter'; }}
};
function $(id) { return elements[id] || null; }
eval(source.slice(start, end));
closeSearchStreamerPicker({returnFocus:true});
const closed = {hidden:elements.searchStreamerPickerPanel.hidden, expanded:attributes['aria-expanded'], focused};
focused = false;
toggleSearchStreamerPicker();
const reopened = {hidden:elements.searchStreamerPickerPanel.hidden, expanded:attributes['aria-expanded'], focused};
process.stdout.write(JSON.stringify({closed, reopened}));
"""
    completed = subprocess.run(
        [NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _evaluate_dashboard_queue_views() -> dict:
    if not NODE:
        raise unittest.SkipTest("Node.js is required for dashboard UI tests")
    runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function dashboardQueueView');
const end = source.indexOf('function dashboardYoutubeView', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Dashboard queue helpers not found');
eval(source.slice(start, end));
const download = {state:'running', job:{type:'download'}, operation:'Downloading'};
const upload = {state:'running', job:{type:'youtube_upload'}, operation:'Uploading to YouTube'};
const waiting = {state:'waiting', job:{type:'download'}, operation:'Waiting to download'};
const failure = {state:'error', resolved:false, job:{type:'youtube_upload'}, operation:'YouTube upload failed'};
const resolved = {state:'error', resolved:true, job:{type:'download'}, operation:'Download failed'};
process.stdout.write(JSON.stringify({
  simultaneous: dashboardQueueView([download, upload, waiting]),
  oneLane: dashboardQueueView([download]),
  idle: dashboardQueueView([]),
  attention: dashboardQueueView([failure, resolved])
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
function streamerAvatarForKnownIdentity(value, size) { return value ? `<avatar data-size="${size}">${value}</avatar>` : ''; }
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
function formatRemainingDuration(value) { return `${Math.ceil(Number(value) / 3600)} hr remaining`; }
function streamerAvatarForKnownIdentity(value, size) { return value ? `<avatar data-size="${size}">${value}</avatar>` : ''; }
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
let streamerProfilesByLogin = new Map();
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
    def test_design_foundation_exposes_semantic_tokens_and_shared_controls(self) -> None:
        for token in (
            "--surface-app:", "--surface-panel:", "--border-default:",
            "--text-primary:", "--text-muted:", "--color-accent:",
            "--color-success:", "--color-warning:", "--color-danger:",
            "--color-info:", "--space-1:", "--radius-md:", "--shadow-sm:",
            "--focus-ring:", "--control-height:",
        ):
            self.assertIn(token, STYLESHEET)
        self.assertIn("@media (prefers-reduced-motion:reduce)", STYLESHEET)
        self.assertIn("button:active:not(:disabled)", STYLESHEET)
        self.assertIn("input:not([type=\"checkbox\"]):not([type=\"radio\"])", STYLESHEET)

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
        # The explicit-click wrapper now owns button state; automatic refreshes do not.
        self.assertFalse(result["updatingDisabled"])
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

        self.assertEqual(result["loading"]["title"], "Automatic Live Recording · Checking…")
        self.assertEqual(result["running"]["title"], "Automatic Live Recording · Running")
        self.assertIn("Watching 4", result["running"]["detail"])
        self.assertIn("Last checked", result["running"]["detail"])
        self.assertEqual(result["paused"]["title"], "Automatic Live Recording · Paused")
        self.assertEqual(result["paused"]["detail"], "4 streamers selected")
        self.assertEqual(result["zero"]["kind"], "running")
        self.assertEqual(result["zero"]["detail"], "No streamers selected")
        self.assertEqual(result["degraded"]["kind"], "degraded")
        self.assertIn("State file invalid", result["degraded"]["detail"])
        self.assertIn("paused for safety", result["degraded"]["detail"])
        self.assertEqual(
            result["failed"]["title"], "Automatic Live Recording status unavailable"
        )
        self.assertNotIn("Paused", result["failed"]["title"])
        self.assertEqual(result["native"]["title"], "Automatic Live Recording · Unavailable")

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
        buttons = re.findall(r'class="nav-btn(?: active)?" data-page="([^"]+)"[^>]*>.*?<span>([^<]+)</span></button>', TEMPLATE)
        self.assertEqual(
            buttons,
            [
                ("dashboard", "Dashboard"),
                ("search", "VODs"),
                ("live", "Live"),
                ("queue", "Queue"),
                ("settings", "Settings"),
            ],
        )
        live_page = TEMPLATE.split('id="page-live"', 1)[1].split('id="page-search"', 1)[0]
        dashboard_page = TEMPLATE.split('id="page-dashboard"', 1)[1].split('id="page-live"', 1)[0]
        self.assertIn('id="page-live"', TEMPLATE)
        self.assertNotIn("Live management is moving here", TEMPLATE)
        self.assertIn('id="liveStreamsSection"', live_page)
        self.assertIn('id="liveActiveRecordings"', live_page)
        self.assertIn('id="autoRecorderStatus"', live_page)
        self.assertIn('id="refreshLiveStatuses"', live_page)
        self.assertNotIn('id="liveStreamsSection"', dashboard_page)
        self.assertNotIn('id="liveStreamsList"', dashboard_page)
        self.assertIn('id="dashboardLiveSummary"', dashboard_page)
        self.assertIn("No active recordings.", live_page)
        self.assertEqual(TEMPLATE.count('id="liveStreamsList"'), 1)
        self.assertEqual(TEMPLATE.count('id="autoRecorderStatus"'), 1)
        self.assertEqual(TEMPLATE.count('id="refreshLiveStatuses"'), 1)
        self.assertIn("search: ['VODs', 'Find, download, and manage Twitch VODs.']", JAVASCRIPT)
        self.assertIn("live: ['Live', 'Monitor live streamers and recordings.']", JAVASCRIPT)
        self.assertIn("btn.setAttribute('aria-current', 'page')", JAVASCRIPT)
        self.assertIn('aria-current="page"', TEMPLATE)
        self.assertNotIn("Ready for another VOD?", TEMPLATE)
        self.assertNotIn('id="dashboardIdle"', TEMPLATE)

    def test_mobile_navigation_drawer_has_keyboard_and_backdrop_hooks(self) -> None:
        self.assertIn('id="sidebarBackdrop"', TEMPLATE)
        self.assertIn('aria-controls="appSidebar" aria-expanded="false"', TEMPLATE)
        self.assertIn("const closeMobileNav", JAVASCRIPT)
        self.assertIn("const openMobileNav", JAVASCRIPT)
        self.assertIn("event.key === 'Escape'", JAVASCRIPT)
        self.assertIn("backdrop.addEventListener('click'", JAVASCRIPT)
        self.assertIn("firstNavItem.focus()", JAVASCRIPT)
        self.assertIn("if (returnFocus) toggle.focus()", JAVASCRIPT)
        self.assertIn(".sidebar-backdrop", STYLESHEET)

    def test_live_workspace_uses_dense_rows_and_a_compact_recording_empty_state(self) -> None:
        self.assertIn('active-recordings-section is-empty', TEMPLATE)
        self.assertIn("const activeSection = activeBox?.closest('.active-recordings-section');", JAVASCRIPT)
        self.assertIn("activeSection?.classList.toggle('is-empty', !activeRecordings.length);", JAVASCRIPT)
        self.assertIn('class="live-stream-primary"', JAVASCRIPT)
        self.assertIn('class="live-stream-details"', JAVASCRIPT)
        self.assertIn('class="live-stream-footer"', JAVASCRIPT)
        self.assertIn('class="live-stream-metadata"', JAVASCRIPT)
        self.assertIn("No active recordings.", TEMPLATE + JAVASCRIPT)
        self.assertIn("#page-live .live-stream-grid { grid-template-columns:minmax(0, 1fr);", STYLESHEET)
        self.assertIn("#page-live .active-recordings-section.is-empty", STYLESHEET)
        self.assertIn("#page-live .live-stream-footer { display:grid;", STYLESHEET)
        self.assertIn("#page-live .live-stream-actions button {\n    width:auto;", STYLESHEET)
        self.assertIn("#page-live .live-stream-name { min-width:0;", STYLESHEET)

    def test_settings_sections_replace_old_primary_pages(self) -> None:
        self.assertEqual(
            re.findall(r'data-settings-tab="([^"]+)"', TEMPLATE),
            ["general", "automation", "streamers", "youtube", "advanced"],
        )
        self.assertNotIn('id="page-youtube"', TEMPLATE)
        self.assertNotIn('id="page-localuploads"', TEMPLATE)
        self.assertNotIn('id="page-streamers"', TEMPLATE)

    def test_settings_tabs_have_complete_accessible_panel_relationships(self) -> None:
        for name in ("General", "Automation", "Streamers", "Youtube", "Advanced"):
            self.assertRegex(
                TEMPLATE,
                rf'id="settingsTab{name}"[^>]*role="tab"[^>]*aria-controls="settingsPanel{name}"',
            )
            self.assertRegex(
                TEMPLATE,
                rf'id="settingsPanel{name}"[^>]*role="tabpanel"[^>]*aria-labelledby="settingsTab{name}"',
            )
        tab_switcher = JAVASCRIPT.split("function showSettingsTab", 1)[1].split("function updateAutoRecorderSettingCopy", 1)[0]
        self.assertIn("panel.hidden = !active", tab_switcher)
        self.assertIn("tab.setAttribute('aria-selected'", tab_switcher)
        self.assertIn("tab.setAttribute('tabindex', active ? '0' : '-1')", tab_switcher)
        self.assertIn("ensureActiveSettingsTabVisible(activeTab);", tab_switcher)
        visibility_helper = JAVASCRIPT.split("function ensureActiveSettingsTabVisible", 1)[1].split("function showSettingsTab", 1)[0]
        self.assertIn("strip.scrollLeft", visibility_helper)
        self.assertNotIn("scrollIntoView", visibility_helper)
        self.assertIn("['ArrowLeft', 'ArrowRight', 'Home', 'End']", JAVASCRIPT)
        self.assertIn(".settings-panel[hidden] { display:none !important; }", STYLESHEET)

    def test_automation_workspace_maps_only_global_controls_to_legacy_fields(self) -> None:
        automation = TEMPLATE.split('data-settings-panel="automation"', 1)[1].split('data-settings-panel="streamers"', 1)[0]
        for heading in (
            "VOD Monitoring",
            "Automatic YouTube Processing",
            "Automatic Live Recording",
            "Automated Upload Retention",
        ):
            self.assertIn(heading, automation)
        for control in (
            "autoVodEnabled",
            "autoVodPollMinutes",
            "autoYoutubeEnabled",
            "autoRecorderEnabled",
            "autoYoutubeCleanupDelayHours",
        ):
            self.assertIn(f'id="{control}"', automation)
        saver = JAVASCRIPT.split("async function saveAutomationSettings", 1)[1].split("function parseProgress", 1)[0]
        for field in (
            "auto_vod_enabled",
            "auto_vod_poll_minutes",
            "auto_youtube_enabled",
            "auto_recorder_enabled",
            "auto_youtube_cleanup_delay_hours",
        ):
            self.assertIn(field, saver)
        self.assertNotIn("streamer_profiles", saver)
        self.assertIn("Streamer policies were not changed.", saver)

    def test_streamer_workspace_is_canonical_compact_and_mobile_safe(self) -> None:
        renderer = JAVASCRIPT.split("function renderStreamerEditor", 1)[1].split("async function saveStreamerPolicy", 1)[0]
        self.assertIn("streamerWorkspaceEntries(", renderer)
        self.assertIn("streamer-policy-summary", renderer)
        self.assertIn("validationLabel", renderer)
        self.assertIn("expandedStreamerLogin === login", renderer)
        self.assertIn("streamerPolicyEditorDirty", renderer)
        self.assertIn("data-streamer-action=\"up\"", renderer)
        self.assertIn("data-streamer-action=\"down\"", renderer)
        self.assertIn("data-streamer-action=\"remove\"", renderer)
        self.assertIn("aria-expanded", renderer)
        self.assertIn("@media (max-width:430px)", STYLESHEET)
        self.assertIn(".streamer-policy-editor select { min-height:44px; }", STYLESHEET)
        self.assertIn(".streamer-policy-summary { grid-column:1 / -1; grid-template-columns:repeat(3,minmax(0,1fr));", STYLESHEET)

    def test_streamer_discovery_search_and_canonical_policy_filters(self) -> None:
        result = _evaluate_streamer_workspace_filters()

        self.assertEqual(
            result["all"],
            ["Manual", "AutoDownload", "AutoYoutube", "LiveBravo", "ReviewCase"],
        )
        self.assertEqual(result["automated"], ["AutoDownload", "AutoYoutube"])
        self.assertEqual(result["live"], ["LiveBravo"])
        self.assertEqual(result["review"], ["ReviewCase"])
        self.assertEqual(result["caseInsensitive"], ["AutoDownload", "AutoYoutube"])
        self.assertEqual(result["combined"], ["AutoYoutube"])
        self.assertEqual(result["none"], [])
        self.assertEqual(result["before"], result["after"])

    def test_streamer_discovery_controls_preserve_order_and_editor_state(self) -> None:
        self.assertIn('id="streamerListSearch"', TEMPLATE)
        self.assertIn('aria-label="Filter streamers"', TEMPLATE)
        for filter_name in ("all", "automated", "live-recording", "needs-review"):
            self.assertIn(f'data-streamer-filter="{filter_name}"', TEMPLATE)
        renderer = JAVASCRIPT.split("function renderStreamerEditor", 1)[1].split("async function saveStreamerPolicy", 1)[0]
        self.assertIn("streamerWorkspaceEntries(", renderer)
        self.assertIn("No streamers match these filters.", renderer)
        self.assertIn("data-streamer-index=\"${index}\"", renderer)
        self.assertIn("const current = streamerEditorNames();", renderer)
        self.assertIn("captureExpandedStreamerPolicyDraft();", JAVASCRIPT)
        self.assertIn("streamerPolicyEditorDraft", JAVASCRIPT)
        self.assertIn("streamerPolicyEditorDraft.delete(login)", JAVASCRIPT)
        self.assertIn("streamerListSearchQuery", JAVASCRIPT)
        self.assertIn("streamerListFilter", JAVASCRIPT)

    def test_streamer_discovery_has_compact_mobile_hooks(self) -> None:
        self.assertIn(".streamer-discovery-controls", STYLESHEET)
        self.assertIn(".streamer-filter-chips", STYLESHEET)
        self.assertIn("overflow-x:auto", STYLESHEET)
        self.assertIn(".streamer-discovery-controls { grid-template-columns:1fr;", STYLESHEET)
        self.assertIn("event.key !== 'Escape'", JAVASCRIPT)

    def test_streamer_avatar_loader_and_renderer_are_shared_and_safe(self) -> None:
        result = _evaluate_streamer_avatar_ui()

        self.assertEqual(result["callsAfterLoad"], 1)
        self.assertEqual(result["profileKeys"], [])
        self.assertIn('src="/api/streamer-avatar/nika_livetv"', result["imageHtml"])
        self.assertIn('alt=""', result["imageHtml"])
        self.assertIn('aria-hidden="true"', result["imageHtml"])
        self.assertIn("NL", result["imageHtml"])
        self.assertNotIn("streamer-avatar-image", result["missingHtml"])
        self.assertIn(">D<", result["missingHtml"])
        self.assertNotIn("streamer-avatar-image", result["noImageHtml"])
        self.assertIn(">NI<", result["noImageHtml"])
        self.assertNotIn("streamer-avatar-image", result["failureHtml"])
        self.assertTrue(result["removed"])
        self.assertIn("function loadStreamerProfiles()", JAVASCRIPT)
        self.assertIn("function streamerAvatarHtml", JAVASCRIPT)
        self.assertIn("function wireStreamerAvatarFallbacks", JAVASCRIPT)
        self.assertIn(".streamer-avatar-image", STYLESHEET)
        self.assertIn("object-fit:cover", STYLESHEET)

    def test_avatar_hooks_reuse_the_shared_component_on_approved_identity_rows(self) -> None:
        streamer_renderer = JAVASCRIPT.split("function renderStreamerEditor", 1)[1].split("async function saveStreamerPolicy", 1)[0]
        live_renderer = JAVASCRIPT.split("function renderLiveStreamCard", 1)[1].split("function syncLiveStreamers", 1)[0]
        picker_renderer = JAVASCRIPT.split("function renderSearchStreamerCheckboxes", 1)[1].split("function selectedSearchStreamersFromCheckboxes", 1)[0]
        result_renderer = JAVASCRIPT.split("function renderResults", 1)[1].split("async function searchVods", 1)[0]
        local_renderer = JAVASCRIPT.split("function renderLocalVideoCard", 1)[1].split("function visibleLocalVideoRows", 1)[0]
        queue_renderer = JAVASCRIPT.split("function renderQueueVodItem", 1)[1].split("function renderQueueGroup", 1)[0]
        self.assertIn("streamerAvatarHtml(name, 'compact')", streamer_renderer)
        self.assertIn("streamerAvatarHtml(streamer, 'live')", live_renderer)
        self.assertIn("streamerAvatarHtml(streamer, 'small')", live_renderer)
        self.assertIn("streamerAvatarHtml(s, 'picker')", picker_renderer)
        self.assertIn("streamerAvatarForKnownIdentity(streamer, 'group')", result_renderer)
        self.assertIn("streamerAvatarForKnownIdentity(v.streamer, 'local')", local_renderer)
        self.assertIn("streamerAvatarForKnownIdentity(item.streamer, 'queue')", queue_renderer)
        self.assertIn("wireStreamerAvatarFallbacks(list)", streamer_renderer)
        self.assertIn("actionRoots.forEach(wireStreamerAvatarFallbacks)", live_renderer)
        self.assertIn("wireStreamerAvatarFallbacks(box)", picker_renderer)
        self.assertIn("wireStreamerAvatarFallbacks(body)", result_renderer)
        self.assertIn("data-streamer-action=\"up\"", streamer_renderer)
        self.assertIn("data-streamer-action=\"down\"", streamer_renderer)
        self.assertIn("data-streamer-action=\"remove\"", streamer_renderer)
        self.assertIn("live-recording-start", live_renderer)
        self.assertIn("live-recording-stop", live_renderer)
        self.assertEqual(JAVASCRIPT.count("function loadStreamerProfiles()"), 1)
        self.assertNotIn("https://api.twitch.tv", JAVASCRIPT)
        self.assertIn(".streamer-avatar-picker", STYLESHEET)
        self.assertIn(".streamer-avatar-queue", STYLESHEET)

    def test_shared_toast_foundation_is_safe_accessible_and_migrates_playlist_success(self) -> None:
        result = _evaluate_toast_ui()

        self.assertEqual(result["beforeDismiss"], 4)
        self.assertEqual(result["afterDismiss"], 3)
        self.assertEqual(result["afterTimeout"], 2)
        self.assertEqual(result["success"], {"role":"status", "live":"polite", "text":"<b>safe text</b>", "close":"Dismiss notification"})
        self.assertEqual(result["error"], {"role":"alert", "live":"assertive"})
        self.assertTrue(any("is-info" in value for value in result["variants"]))
        self.assertTrue(any("is-error" in value for value in result["variants"]))
        self.assertIn('id="appToastContainer"', TEMPLATE)
        self.assertIn("textContent = text", JAVASCRIPT)
        self.assertIn("TOAST_TIMEOUTS", JAVASCRIPT)
        self.assertIn(".app-toast-container", STYLESHEET)
        self.assertIn("@media (prefers-reduced-motion:reduce)", STYLESHEET)
        self.assertIn("showToast('Playlists loaded.', {variant:'success'})", JAVASCRIPT)
        self.assertNotIn("then(() => alert('Playlists loaded.'))", JAVASCRIPT)
        self.assertEqual(JAVASCRIPT.count("alert("), 25)
        self.assertEqual(JAVASCRIPT.count("confirm("), 0)

    def test_slice_11b1_migrates_only_low_risk_success_and_info_alerts(self) -> None:
        self.assertIn("showToast(`Download queue started: ${data.url_count || selected.length} VOD(s). Mode: ${batchModeLabel}`, {variant:'success'})", JAVASCRIPT)
        self.assertIn("showToast('The final YouTube filename template was reset.', {variant:'info'})", JAVASCRIPT)
        self.assertIn("showToast('The technical yt-dlp output template was reset.', {variant:'info'})", JAVASCRIPT)
        self.assertNotIn("alert(`Download queue started:", JAVASCRIPT)
        self.assertNotIn("alert('The final YouTube filename template was reset:", JAVASCRIPT)
        self.assertNotIn("alert('The technical yt-dlp output template was reset:", JAVASCRIPT)
        # Confirmation, validation, diagnostic, OAuth, and operational alerts remain guarded.
        self.assertIn("title:'Start selected downloads'", JAVASCRIPT)
        self.assertIn("alert('YouTube connected", JAVASCRIPT)
        self.assertEqual(JAVASCRIPT.count("confirm("), 0)

    def test_slice_11b2_migrates_only_simple_nonblocking_error_alerts(self) -> None:
        self.assertIn(".catch(e => showToast(e.message, {variant:'error'}))", JAVASCRIPT)
        self.assertIn("checkStreamerFileStatus().catch(err => showToast(err.message, {variant:'error'}))", JAVASCRIPT)
        self.assertNotIn("loadYoutubePlaylists().then(() => showToast('Playlists loaded.', {variant:'success'})).catch(e => alert(e.message))", JAVASCRIPT)
        self.assertNotIn("checkStreamerFileStatus().catch(err => alert(err.message))", JAVASCRIPT)
        # Inline search/local-media errors and operational/security alerts retain their destinations.
        self.assertIn("$('searchErrors').innerHTML", JAVASCRIPT)
        self.assertIn("if (errorBox) { errorBox.hidden = false;", JAVASCRIPT)
        self.assertIn("withButtonPending(uploadBtn, {pendingLabel:'Adding to Queue...'}, uploadSelectedLocalVideos)", JAVASCRIPT)
        self.assertIn(".catch(e => alert(e.message));", JAVASCRIPT)
        self.assertIn("alert('YouTube connection failed:", JAVASCRIPT)
        self.assertEqual(JAVASCRIPT.count("alert("), 25)
        self.assertEqual(JAVASCRIPT.count("confirm("), 0)

    def test_shared_confirmation_dialog_is_accessible_safe_and_reentrant(self) -> None:
        result = _evaluate_confirmation_dialog_ui()

        self.assertEqual(result["opened"], {
            "open": True, "focused": "appConfirmDialogCancel", "title": "Delete local VOD",
            "message": "<b>Unsafe</b>", "confirm": "Delete VOD", "className": "danger-outline",
        })
        self.assertFalse(result["duplicateResult"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["afterConfirm"], {"open": False, "focusReturned": True})
        self.assertFalse(result["cancelled"])
        self.assertFalse(result["escaped"])
        self.assertEqual(result["tabFocus"], "appConfirmDialogCancel")
        self.assertTrue(result["tabPrevented"])
        self.assertIn('id="appConfirmDialog"', TEMPLATE)
        self.assertIn('role="dialog"', TEMPLATE)
        self.assertIn('aria-modal="true"', TEMPLATE)
        self.assertIn('aria-labelledby="appConfirmDialogTitle"', TEMPLATE)
        self.assertIn('aria-describedby="appConfirmDialogDescription"', TEMPLATE)

    def test_slice_11c_migrates_all_native_confirmation_guards(self) -> None:
        self.assertEqual(JAVASCRIPT.count("confirm("), 0)
        self.assertIn("title:'Start selected downloads'", JAVASCRIPT)
        self.assertIn("title:action === 'add-auto-youtube-playlist' ? 'Add to YouTube playlist' : 'Start YouTube upload'", JAVASCRIPT)
        self.assertIn("title:'Mark upload complete'", JAVASCRIPT)
        self.assertIn("title:'Delete local VOD'", JAVASCRIPT)
        self.assertIn("confirmLabel:'Delete VOD'", JAVASCRIPT)
        self.assertIn("variant:'danger'", JAVASCRIPT)
        self.assertIn("if (!confirmed) return;", JAVASCRIPT)
        self.assertIn("await api('/api/local-video/delete'", JAVASCRIPT)

    def test_shared_button_pending_feedback_preserves_state_and_prevents_duplicates(self) -> None:
        result = _evaluate_button_pending_ui()

        self.assertEqual(result["during"], {
            "label": "Refreshing...", "disabled": True, "busy": "true", "samePromise": True, "calls": 1,
        })
        self.assertEqual(result["afterSuccess"], {
            "result": "done", "label": "Refresh status", "disabled": False, "busy": None, "calls": 1,
        })
        self.assertEqual(result["afterFailure"], {
            "failure": "status unavailable", "label": "Check settings file", "disabled": False, "busy": None,
        })
        self.assertEqual(result["unavailable"], {"label": "Refresh Playlists", "disabled": True, "calls": 0})

    def test_slice_11d1_uses_pending_feedback_only_for_short_refresh_and_check_actions(self) -> None:
        self.assertIn("withButtonPending($('refreshLiveStatuses'), {pendingLabel:'Refreshing...'}", JAVASCRIPT)
        self.assertIn("withButtonPending($('youtubeLoadPlaylists'), {pendingLabel:'Refreshing...'}", JAVASCRIPT)
        self.assertIn("withButtonPending(button, {pendingLabel:'Checking...'}", JAVASCRIPT)
        self.assertIn("showToast('Playlists loaded.', {variant:'success'})", JAVASCRIPT)
        self.assertIn("showToast(e.message, {variant:'error'})", JAVASCRIPT)
        self.assertIn("fetch('/api/settings/status')", JAVASCRIPT)
        self.assertIn("if (liveStatusRefreshPromise) return liveStatusRefreshPromise;", JAVASCRIPT)
        self.assertIn("$('saveAutomationSettings').addEventListener('click', saveAutomationSettings);", JAVASCRIPT)
        self.assertIn("queue-lane-action", JAVASCRIPT)

    def test_slice_11d2_settings_save_feedback_is_inline_and_consistent(self) -> None:
        for status_id in ("generalSaveStatus", "automationSaveStatus", "youtubeSaveStatus", "advancedSaveStatus"):
            status_markup = TEMPLATE.split(f'id="{status_id}"', 1)[1].split('</p>', 1)[0]
            self.assertIn('No unsaved changes.', status_markup)
        self.assertIn("setScopeStatus(scope, 'Saving...')", JAVASCRIPT)
        self.assertIn("setScopeStatus(scope, 'Saved.')", JAVASCRIPT)
        self.assertIn("setScopeStatus(scope, 'Save failed: ' + (e.message || 'Unable to save settings.'))", JAVASCRIPT)
        self.assertIn("return withButtonPending(btn, {pendingLabel:'Saving...'}", JAVASCRIPT)
        self.assertIn("return withButtonPending(button, {pendingLabel:'Saving...'}", JAVASCRIPT)
        self.assertIn("status.textContent = 'Unsaved changes.'", JAVASCRIPT)
        self.assertIn("status.textContent = streamerListDirty ? 'Unsaved changes.' : 'No unsaved changes.'", JAVASCRIPT)
        self.assertIn("status.textContent = 'Saved.'", JAVASCRIPT)
        self.assertIn("status.textContent = 'Save failed: ' + (error.message", JAVASCRIPT)
        self.assertNotIn("showToast('Automation settings saved.')", JAVASCRIPT)
        self.assertNotIn("showToast(`${saved.count || 0} streamer", JAVASCRIPT)
        self.assertNotIn("showToast(`${name} policy saved.`)", JAVASCRIPT)
        self.assertIn("button.setAttribute('aria-busy', 'true')", JAVASCRIPT)
        self.assertIn("Save failed: ' + (error.message || 'Automation settings could not be saved.')", JAVASCRIPT)

    def test_slice_11d2_preserves_settings_save_and_confirm_boundaries(self) -> None:
        self.assertEqual(JAVASCRIPT.count("confirm("), 0)
        self.assertIn("alert(label + ' saved.", JAVASCRIPT)
        self.assertIn("alert('Save failed:\\n\\n' + e.message)", JAVASCRIPT)
        streamer_save = JAVASCRIPT.split("$('saveStreamers').addEventListener('click'", 1)[1].split("$('autoRecorderEnabled')", 1)[0]
        self.assertIn("api('/api/streamers'", streamer_save)
        self.assertRegex(streamer_save, r"streamers\s*:\s*\$\('streamersText'\)\.value")
        self.assertRegex(streamer_save, r"streamer_profiles\s*:\s*streamerProfileDraft")

    def test_slice_11d3_keeps_operational_lifecycle_feedback_in_its_workspace(self) -> None:
        live_actions = JAVASCRIPT.split("async function startLiveRecording", 1)[1].split("const STREAMER_LIST_FILTERS", 1)[0]
        self.assertIn("liveRecordingActions.set(login, {phase:'starting'})", live_actions)
        self.assertIn("liveRecordingActions.set(login, {phase:'stopping'})", live_actions)
        self.assertIn("pollJobs().catch(() => {})", live_actions)

        queue_actions = JAVASCRIPT.split("function wireQueueItemInteractions", 1)[1].split("function friendlyQueueActionError", 1)[0]
        self.assertIn("pendingAutoYoutubeReleases", queue_actions)
        self.assertIn("pendingAutoYoutubePlaylistActions", queue_actions)
        self.assertIn("if (!confirmed) return;", queue_actions)
        self.assertIn("button.disabled = true;", queue_actions)

        lane_controls = JAVASCRIPT.split("function renderQueueLaneControls", 1)[1].split("function queueHistoryTimestamp", 1)[0]
        self.assertIn("withButtonPending(button, {pendingLabel:action === 'pause' ? 'Pausing...' : 'Resuming...'}", lane_controls)
        self.assertIn("await pollJobs();", lane_controls)
        self.assertNotIn("Active work continues; no new item will start.", lane_controls)

        self.assertIn("withButtonPending(btn, {pendingLabel:'Adding...'}", JAVASCRIPT)
        self.assertIn("withButtonPending(uploadBtn, {pendingLabel:'Adding to Queue...'}, uploadSelectedLocalVideos)", JAVASCRIPT)
        self.assertIn("showPage('queue');", JAVASCRIPT)

    def test_mobile_toast_placement_is_bottom_anchored_and_stacks_upward(self) -> None:
        self.assertIn(".app-toast-container { top:auto; right:max(12px,env(safe-area-inset-right));", STYLESHEET)
        self.assertIn("bottom:calc(12px + env(safe-area-inset-bottom))", STYLESHEET)
        self.assertIn("left:max(12px,env(safe-area-inset-left))", STYLESHEET)
        self.assertIn("flex-direction:column-reverse", STYLESHEET)
        self.assertIn("max-height:calc(100dvh - 24px", STYLESHEET)
        self.assertIn("overflow-y:auto", STYLESHEET)
        self.assertIn("@media (max-width:430px)", STYLESHEET)

    def test_local_vod_avatar_requires_a_reliable_streamer_identity(self) -> None:
        known = {
            "path": "C:/media/cptmary/known.mp4", "name": "known.mp4",
            "streamer": "cptmary", "local_file_exists": True,
        }
        unknown = {
            "path": "C:/media/unknown.mp4", "name": "unknown.mp4",
            "streamer": "", "local_file_exists": True,
        }
        known_html = _evaluate_local_history_ui(known, [known])["card"]
        unknown_html = _evaluate_local_history_ui(unknown, [unknown])["card"]

        self.assertIn('<avatar data-size="local">cptmary</avatar>', known_html)
        self.assertNotIn('<avatar', unknown_html)
        self.assertIn("localvideocheck", known_html)
        self.assertIn("localvideocheck", unknown_html)

    def test_manual_download_workflow_keeps_both_legacy_gates_separate(self) -> None:
        general = TEMPLATE.split('data-settings-panel="general"', 1)[1].split('data-settings-panel="automation"', 1)[0]
        self.assertIn("Manual Download Workflow", general)
        self.assertIn("After a manually started download", general)
        self.assertIn('id="youtubeEnabled"', general)
        self.assertIn('id="youtubeAutoUpload"', general)
        self.assertNotIn("Enable YouTube Uploads", TEMPLATE)
        synchronizer = JAVASCRIPT.split("function syncManualDownloadWorkflowMode", 1)[1].split("function applyManualDownloadWorkflowChoice", 1)[0]
        self.assertIn("manual_download_workflow", synchronizer)
        self.assertIn("blocked_by_legacy_youtube_gate", synchronizer)
        updater = JAVASCRIPT.split("function applyManualDownloadWorkflowChoice", 1)[1].split("function updateStreamerListSaveState", 1)[0]
        self.assertIn("youtubeEnabled", updater)
        self.assertIn("youtubeAutoUpload", updater)

    def test_normal_ui_has_no_desktop_file_manager_actions(self) -> None:
        for label in (
            "Open Download Folder",
            "Show in Folder",
            "Open TXT",
            "YouTube Studio + Show in Folder",
        ):
            self.assertNotIn(label, TEMPLATE)

    def test_queue_exposes_process_oriented_sections_and_no_local_media_workspace(self) -> None:
        queue_page = TEMPLATE.split('id="page-queue"', 1)[1].split('id="page-settings"', 1)[0]
        for heading in ("Running", "Up Next", "Needs Attention"):
            self.assertIn(f">{heading}<", queue_page)
        self.assertIn("<summary>Completed ", queue_page)
        self.assertNotIn("Ready for Upload", queue_page)
        self.assertNotIn("Local VODs", queue_page)
        self.assertNotIn("localVideoCards", queue_page)
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
        lane_view = JAVASCRIPT.split("function queueLaneControlView", 1)[1].split("function renderQueueLaneControls", 1)[0]
        lane_controls = JAVASCRIPT.split("function renderQueueLaneControls", 1)[1].split("function queueHistoryTimestamp", 1)[0]
        self.assertIn("label:paused ? 'Resume Queue' : 'Pause Queue'", lane_view)
        self.assertIn("Object.prototype.hasOwnProperty.call(queueControls, lane)", lane_controls)
        self.assertIn("const view = queueLaneControlView(control, known)", lane_controls)
        self.assertIn("${view.label}", lane_controls)
        self.assertRegex(lane_controls, r"/api/queue/\$\{action\}")
        self.assertIn("withButtonPending(button,", lane_controls)
        self.assertIn("Pausing...", lane_controls)
        self.assertIn("Resuming...", lane_controls)
        api_call = lane_controls.index("await api(")
        poll_call = lane_controls.index("await pollJobs()")
        self.assertLess(api_call, poll_call)
        self.assertIn("showToast(error.message, 'bad')", lane_controls)
        self.assertNotIn("Active work continues; no new item will start.", lane_controls)

    def test_queue_operations_workspace_separates_lanes_and_is_media_free(self) -> None:
        queue_page = TEMPLATE.split('id="page-queue"', 1)[1].split('id="page-settings"', 1)[0]
        for element_id in (
            "queueOperationalSummary",
            "queueRunningDownloadsLane",
            "queueRunningUploadsLane",
            "queueWaitingSection",
            "queueWaitingDownloadsLane",
            "queueWaitingUploadsLane",
            "queueErrorsSection",
            "queueCompletedDetails",
            "queueCancelledDetails",
        ):
            self.assertIn(f'id="{element_id}"', queue_page)
        for local_media_id in (
            "readyForUploadSection",
            "localVideoCards",
            "uploadSelectedLocalVideos",
            "includeUploadedLocalVideos",
        ):
            self.assertNotIn(local_media_id, queue_page)
        self.assertIn("function queueOperationsView", JAVASCRIPT)
        self.assertIn("function renderQueueOperationLanes", JAVASCRIPT)
        self.assertIn("setQueueWorkspaceVisibility('queueWaitingSection', hasWaiting)", JAVASCRIPT)
        self.assertIn("setQueueWorkspaceVisibility('queueErrorsSection', errors.length > 0)", JAVASCRIPT)
        self.assertIn("function queueLaneControlView", JAVASCRIPT)
        self.assertIn("Processing enabled", JAVASCRIPT)
        self.assertIn("$('queueActive')", JAVASCRIPT)
        self.assertIn(".queue-running-lanes", STYLESHEET)
        self.assertIn(".queue-waiting-lanes", STYLESHEET)
        self.assertIn(".queue-section[hidden]", STYLESHEET)
        self.assertIn(".running-estimate[hidden] { display:none !important; }", STYLESHEET)
        self.assertIn(".queue-running-section .section-count[hidden] { display:none !important; }", STYLESHEET)

    def test_queue_operations_view_preserves_parallel_lanes_and_waiting_order(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Queue workspace UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function queueOperationsView');
const end = source.indexOf('function setQueueWorkspaceVisibility', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Queue operations helper not found');
eval(source.slice(start, end));
const download = {state:'running', job:{type:'download', id:'download-1'}};
const upload = {state:'running', job:{type:'youtube_upload', id:'upload-1'}};
const waitingDownload = {state:'waiting', job:{type:'download', id:'download-2'}};
const waitingUpload = {state:'waiting', job:{type:'youtube_upload', id:'upload-2'}};
const failed = {state:'error', resolved:false, job:{type:'youtube_upload', id:'upload-3'}};
const view = queueOperationsView(
  [download, upload, waitingDownload, waitingUpload, failed],
  {download:{queue_paused:false}, youtube_upload:{queue_paused:true}}
);
process.stdout.write(JSON.stringify({
  active:view.active.map(item => item.job.id),
  downloadWaiting:view.download.waiting.map(item => item.job.id),
  uploadWaiting:view.upload.waiting.map(item => item.job.id),
  errors:view.errors.map(item => item.job.id),
  uploadPaused:view.upload.control.queue_paused
}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        view = json.loads(completed.stdout)
        self.assertEqual(view["active"], ["download-1", "upload-1"])
        self.assertEqual(view["downloadWaiting"], ["download-2"])
        self.assertEqual(view["uploadWaiting"], ["upload-2"])
        self.assertEqual(view["errors"], ["upload-3"])
        self.assertTrue(view["uploadPaused"])

    def test_queue_lane_control_copy_distinguishes_processing_from_active_work(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Queue workspace UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function queueLaneControlView');
const end = source.indexOf('function renderQueueLaneControls', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Queue control helper not found');
eval(source.slice(start, end));
process.stdout.write(JSON.stringify({
  enabled:queueLaneControlView({queue_paused:false}, true),
  paused:queueLaneControlView({queue_paused:true}, true),
  unavailable:queueLaneControlView({}, false)
}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered["enabled"]["note"], "Processing enabled")
        self.assertEqual(rendered["enabled"]["action"], "pause")
        self.assertEqual(rendered["paused"]["note"], "Paused")
        self.assertEqual(rendered["paused"]["action"], "resume")
        self.assertEqual(rendered["unavailable"]["note"], "Lane control unavailable")

    def test_queue_idle_estimate_is_hidden_without_an_empty_marker(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Queue workspace UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function formatRemainingDuration');
const end = source.indexOf('function niceStatus', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Queue estimate renderer not found');
const classes = new Set();
const estimate = {hidden:false, innerHTML:'', classList:{toggle(name, force) { if (force) classes.add(name); else classes.delete(name); }}};
function $(id) { return id === 'queueRunningEstimate' ? estimate : null; }
function escapeHtml(value) { return String(value); }
eval(source.slice(start, end));
renderOverallRunningEstimate([]);
const idle = {hidden:estimate.hidden, html:estimate.innerHTML, hiddenClass:classes.has('hidden')};
renderOverallRunningEstimate([{state:'running', etaSeconds:60}]);
const active = {hidden:estimate.hidden, html:estimate.innerHTML, hiddenClass:classes.has('hidden')};
process.stdout.write(JSON.stringify({idle, active}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertTrue(rendered["idle"]["hidden"])
        self.assertTrue(rendered["idle"]["hiddenClass"])
        self.assertEqual(rendered["idle"]["html"], "")
        self.assertFalse(rendered["active"]["hidden"])
        self.assertFalse(rendered["active"]["hiddenClass"])
        self.assertIn("Estimated completion", rendered["active"]["html"])

    def test_queue_lane_renderer_hides_empty_up_next_and_restores_waiting_lanes(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Queue workspace UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function queueOperationsView');
const end = source.indexOf('function renderVodQueue', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Queue lane renderer not found');
function element() {
  const classes = new Set();
  return {
    hidden:false, textContent:'', innerHTML:'',
    classList:{
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); }
    }
  };
}
const elements = Object.fromEntries([
  'queueOperationalSummary', 'queueRunningDownloadsLane', 'queueRunningUploadsLane',
  'queueRunningIdle', 'queueRunningDownloadsCount', 'queueRunningUploadsCount',
  'queueRunningSection', 'queueRunning', 'queueActive',
  'queueWaitingSection', 'queueWaitingDownloadsLane', 'queueWaitingUploadsLane',
  'queueWaitingDownloadsCount', 'queueWaitingUploadsCount',
  'queueRunningDownloads', 'queueRunningUploads', 'queueWaitingDownloads', 'queueWaitingUploads'
].map(id => [id, element()]));
function $(id) { return elements[id] || null; }
function escapeHtml(value) { return String(value); }
function renderQueueGroup(id, items) { elements[id].items = items; }
eval(source.slice(start, end));
const controls = {download:{queue_paused:false}, youtube_upload:{queue_paused:false}};
renderQueueOperationLanes(queueOperationsView([], controls));
const idle = {
  upNextHidden:elements.queueWaitingSection.hidden,
  idleVisible:!elements.queueRunningIdle.hidden,
  activeCountHidden:elements.queueActive.hidden,
  runningSectionCompact:elements.queueRunningSection.classList.contains('is-idle'),
  runningContainerCompact:elements.queueRunning.classList.contains('is-idle'),
  downloadLaneHidden:elements.queueRunningDownloadsLane.hidden,
  uploadLaneHidden:elements.queueRunningUploadsLane.hidden
};
renderQueueOperationLanes(queueOperationsView([
  {state:'waiting', job:{type:'download', id:'download-2'}},
  {state:'waiting', job:{type:'youtube_upload', id:'upload-2'}}
], controls));
const waiting = {
  upNextHidden:elements.queueWaitingSection.hidden,
  downloadLaneHidden:elements.queueWaitingDownloadsLane.hidden,
  uploadLaneHidden:elements.queueWaitingUploadsLane.hidden,
  downloadItems:elements.queueWaitingDownloads.items.map(item => item.job.id),
  uploadItems:elements.queueWaitingUploads.items.map(item => item.job.id)
};
renderQueueOperationLanes(queueOperationsView([
  {state:'running', job:{type:'download', id:'download-1'}},
  {state:'running', job:{type:'youtube_upload', id:'upload-1'}}
], controls));
const active = {
  idleHidden:elements.queueRunningIdle.hidden,
  activeCountHidden:elements.queueActive.hidden,
  runningSectionCompact:elements.queueRunningSection.classList.contains('is-idle'),
  downloadLaneHidden:elements.queueRunningDownloadsLane.hidden,
  uploadLaneHidden:elements.queueRunningUploadsLane.hidden,
  downloadItems:elements.queueRunningDownloads.items.map(item => item.job.id),
  uploadItems:elements.queueRunningUploads.items.map(item => item.job.id)
};
process.stdout.write(JSON.stringify({idle, waiting, active}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertTrue(rendered["idle"]["upNextHidden"])
        self.assertTrue(rendered["idle"]["idleVisible"])
        self.assertTrue(rendered["idle"]["activeCountHidden"])
        self.assertTrue(rendered["idle"]["runningSectionCompact"])
        self.assertTrue(rendered["idle"]["runningContainerCompact"])
        self.assertTrue(rendered["idle"]["downloadLaneHidden"])
        self.assertTrue(rendered["idle"]["uploadLaneHidden"])
        self.assertFalse(rendered["waiting"]["upNextHidden"])
        self.assertFalse(rendered["waiting"]["downloadLaneHidden"])
        self.assertFalse(rendered["waiting"]["uploadLaneHidden"])
        self.assertEqual(rendered["waiting"]["downloadItems"], ["download-2"])
        self.assertEqual(rendered["waiting"]["uploadItems"], ["upload-2"])
        self.assertTrue(rendered["active"]["idleHidden"])
        self.assertFalse(rendered["active"]["activeCountHidden"])
        self.assertFalse(rendered["active"]["runningSectionCompact"])
        self.assertFalse(rendered["active"]["downloadLaneHidden"])
        self.assertFalse(rendered["active"]["uploadLaneHidden"])
        self.assertEqual(rendered["active"]["downloadItems"], ["download-1"])
        self.assertEqual(rendered["active"]["uploadItems"], ["upload-1"])

    def test_dashboard_idle_state_is_intentional_and_compact(self) -> None:
        self.assertIn('id="dashboardRunningSection"', TEMPLATE)
        self.assertIn('id="dashboardUpcomingSection"', TEMPLATE)
        self.assertIn("Nothing is running right now.", JAVASCRIPT)
        self.assertIn("const {active, waiting, idle} = dashboardCurrentActivityState(queueView);", JAVASCRIPT)
        self.assertIn("upcoming.hidden = !waiting.length", JAVASCRIPT)
        self.assertIn("count.hidden = idle", JAVASCRIPT)
        self.assertIn(".heading-count[hidden] { display:none !important; }", STYLESHEET)
        self.assertIn(".dashboard-upcoming[hidden] { display:none !important; }", STYLESHEET)
        self.assertIn("Current Activity", TEMPLATE)

    def test_dashboard_empty_sections_and_overview_layout_have_explicit_hooks(self) -> None:
        self.assertIn("section.hidden = !issues.length", JAVASCRIPT)
        self.assertIn("box.innerHTML = '';", JAVASCRIPT)
        self.assertIn(".dashboard-attention[hidden] { display:none; }", STYLESHEET)
        self.assertIn(".dashboard-overview-grid { display:grid; grid-template-columns:repeat(5, minmax(0,1fr));", STYLESHEET)
        self.assertIn("#dashboardStorageCard { grid-column:1 / -1; min-height:92px; }", STYLESHEET)

    def test_dashboard_activity_state_distinguishes_idle_and_waiting_work(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for dashboard activity UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function dashboardCurrentActivityState');
const end = source.indexOf('function renderDashboardCurrentActivity', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Dashboard activity helper not found');
eval(source.slice(start, end));
process.stdout.write(JSON.stringify({
  idle: dashboardCurrentActivityState({active:[], waiting:[]}),
  waiting: dashboardCurrentActivityState({active:[], waiting:[{id:'waiting'}]}),
  active: dashboardCurrentActivityState({active:[{id:'active'}], waiting:[]})
}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        states = json.loads(completed.stdout)
        self.assertTrue(states["idle"]["idle"])
        self.assertFalse(states["waiting"]["idle"])
        self.assertFalse(states["active"]["idle"])

    def test_dashboard_activity_render_hides_idle_dom_and_restores_waiting_up_next(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for dashboard activity UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function dashboardCurrentActivityState');
const end = source.indexOf('function dashboardAttentionIssues', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Dashboard activity renderer not found');
const elements = {};
for (const id of ['dashboardRunningSection','dashboardRunning','dashboardActivityCount','dashboardUpcomingSection','dashboardUpcoming']) {
  elements[id] = {hidden:false, innerHTML:'', textContent:'', classList:{toggle(){}}};
}
function $(id) { return elements[id]; }
function escapeHtml(value) { return String(value).replace(/[&<>\"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[character])); }
eval(source.slice(start, end));
renderDashboardCurrentActivity({active:[], waiting:[]});
const idle = {countHidden:elements.dashboardActivityCount.hidden, upcomingHidden:elements.dashboardUpcomingSection.hidden, copy:elements.dashboardRunning.innerHTML};
renderDashboardCurrentActivity({active:[], waiting:[{operation:'Waiting', streamer:'demo'}]});
const waiting = {countHidden:elements.dashboardActivityCount.hidden, upcomingHidden:elements.dashboardUpcomingSection.hidden, upcoming:elements.dashboardUpcoming.innerHTML};
process.stdout.write(JSON.stringify({idle, waiting}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertTrue(rendered["idle"]["countHidden"])
        self.assertTrue(rendered["idle"]["upcomingHidden"])
        self.assertEqual(rendered["idle"]["copy"], "Nothing is running right now.")
        self.assertFalse(rendered["waiting"]["countHidden"])
        self.assertFalse(rendered["waiting"]["upcomingHidden"])
        self.assertIn("demo", rendered["waiting"]["upcoming"])

    def test_dashboard_control_center_sections_and_safe_shortcuts(self) -> None:
        dashboard_page = TEMPLATE.split('id="page-dashboard"', 1)[1].split('id="page-live"', 1)[0]
        for section_id in (
            "dashboardOverviewTitle",
            "dashboardAttentionSection",
            "dashboardRunningSection",
            "dashboardLiveTitle",
            "dashboardQuickActionsTitle",
        ):
            self.assertIn(section_id, dashboard_page)
        for label in ("VOD Automation", "Live Recording", "Queue", "YouTube", "Quick Actions"):
            self.assertIn(label, dashboard_page)
        self.assertIn('data-page="search"', dashboard_page)
        self.assertIn('data-page="queue"', dashboard_page)
        self.assertIn('data-page="live"', dashboard_page)
        self.assertNotIn("Start Recording", dashboard_page)
        self.assertNotIn("Stop Recording", dashboard_page)
        self.assertEqual(TEMPLATE.count('id="dashboardAlerts"'), 1)

    def test_dashboard_queue_presentations_cover_parallel_idle_and_attention_states(self) -> None:
        views = _evaluate_dashboard_queue_views()

        self.assertEqual(views["simultaneous"]["title"], "2 running")
        self.assertIn("1 download", views["simultaneous"]["metrics"])
        self.assertIn("1 upload", views["simultaneous"]["metrics"])
        self.assertIn("1 waiting", views["simultaneous"]["metrics"])
        self.assertEqual(views["oneLane"]["title"], "1 running")
        self.assertEqual(views["idle"]["title"], "Healthy")
        self.assertEqual(views["idle"]["detail"], "No active or waiting work.")
        self.assertEqual(views["attention"]["kind"], "degraded")
        self.assertEqual(len(views["attention"]["errors"]), 1)

    def test_dashboard_uses_status_specific_data_without_legacy_automation_presentation(self) -> None:
        self.assertIn("function dashboardVodAutomationView", JAVASCRIPT)
        self.assertIn("title:'Unavailable'", JAVASCRIPT)
        self.assertIn("function dashboardAttentionIssues", JAVASCRIPT)
        self.assertIn("function dashboardLifecycleHtml", JAVASCRIPT)
        self.assertIn("item.job?.origin !== 'auto_youtube'", JAVASCRIPT)
        self.assertIn("function renderDashboardLiveSummary", JAVASCRIPT)
        self.assertIn("renderDashboardVodAutomation();", JAVASCRIPT)
        self.assertIn("renderDashboardLiveRecording();", JAVASCRIPT)
        self.assertIn("renderDashboardLiveSummary();", JAVASCRIPT)

    def test_search_diagnostics_are_not_in_normal_results(self) -> None:
        self.assertIn("Technical search details", TEMPLATE)
        self.assertIn('id="searchDiagnostics"', TEMPLATE)
        self.assertIn("$('searchErrors').innerHTML = errHtml;", JAVASCRIPT)
        self.assertIn("Ready to Download", JAVASCRIPT)
        self.assertNotIn("New/Pending", JAVASCRIPT)

    def test_find_vods_workspace_replaces_the_numbered_search_wizard(self) -> None:
        self.assertIn('id="findVodsTab"', TEMPLATE)
        self.assertIn('id="localVodsTab"', TEMPLATE)
        self.assertIn('id="findVodsPanel"', TEMPLATE)
        self.assertIn('id="localVodsPanel"', TEMPLATE)
        self.assertIn('id="searchStreamerPickerToggle"', TEMPLATE)
        self.assertIn('id="searchStreamerPickerPanel"', TEMPLATE)
        self.assertIn('id="vodFilterDetails"', TEMPLATE)
        self.assertIn('id="singleUrl"', TEMPLATE)
        self.assertNotIn("1. Choose when", TEMPLATE)
        self.assertNotIn("2. Choose who", TEMPLATE)
        self.assertNotIn("3. Find VODs", TEMPLATE)
        self.assertNotIn("4. Select and download", TEMPLATE)
        self.assertNotIn('id="streamerMode"', TEMPLATE)
        self.assertNotIn('id="singleStreamer"', TEMPLATE)

    def test_vod_workspace_tabs_are_mutually_exclusive_and_preserve_find_state(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for VOD tab UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function showVodWorkspaceTab');
const end = source.indexOf('function selectedUrls', start);
if (start < 0 || end < 0 || end <= start) throw new Error('VOD tab helper not found');
function element() {
  const classes = new Set();
  const attributes = new Map();
  return {
    hidden:false,
    classList:{toggle(name, force) { if (force) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); }},
    setAttribute(name, value) { attributes.set(name, String(value)); },
    attribute(name) { return attributes.get(name); }
  };
}
const elements = Object.fromEntries(['findVodsTab', 'localVodsTab', 'findVodsPanel', 'localVodsPanel'].map(id => [id, element()]));
let loads = 0;
function $(id) { return elements[id] || null; }
function loadLocalVideos() { loads += 1; return Promise.resolve(); }
eval(source.slice(start, end));
const searchState = {datePreset:'last-7', streamers:['alpha'], resultCount:3};
showVodWorkspaceTab('local');
const local = {
  findHidden:elements.findVodsPanel.hidden,
  localHidden:elements.localVodsPanel.hidden,
  findSelected:elements.findVodsTab.attribute('aria-selected'),
  localSelected:elements.localVodsTab.attribute('aria-selected'),
  findAriaHidden:elements.findVodsPanel.attribute('aria-hidden'),
  localAriaHidden:elements.localVodsPanel.attribute('aria-hidden'),
  loads
};
showVodWorkspaceTab('find');
const find = {
  findHidden:elements.findVodsPanel.hidden,
  localHidden:elements.localVodsPanel.hidden,
  findSelected:elements.findVodsTab.attribute('aria-selected'),
  localSelected:elements.localVodsTab.attribute('aria-selected'),
  searchState
};
process.stdout.write(JSON.stringify({local, find}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertTrue(rendered["local"]["findHidden"])
        self.assertFalse(rendered["local"]["localHidden"])
        self.assertEqual(rendered["local"]["findSelected"], "false")
        self.assertEqual(rendered["local"]["localSelected"], "true")
        self.assertEqual(rendered["local"]["findAriaHidden"], "true")
        self.assertEqual(rendered["local"]["localAriaHidden"], "false")
        self.assertEqual(rendered["local"]["loads"], 1)
        self.assertFalse(rendered["find"]["findHidden"])
        self.assertTrue(rendered["find"]["localHidden"])
        self.assertEqual(rendered["find"]["findSelected"], "true")
        self.assertEqual(rendered["find"]["localSelected"], "false")
        self.assertEqual(rendered["find"]["searchState"], {"datePreset": "last-7", "streamers": ["alpha"], "resultCount": 3})
        self.assertIn('.vod-workspace-panel[hidden] { display:none !important; }', STYLESHEET)

    def test_vod_date_presets_use_local_calendar_dates(self) -> None:
        ranges = _evaluate_vod_date_presets()

        self.assertEqual(ranges["today"], {"from": "2026-01-01", "to": "2026-01-01"})
        self.assertEqual(ranges["yesterdayToday"], {"from": "2025-12-31", "to": "2026-01-01"})
        self.assertEqual(ranges["last7"], {"from": "2025-12-26", "to": "2026-01-01"})
        self.assertEqual(ranges["last30"], {"from": "2025-12-03", "to": "2026-01-01"})
        helper_source = JAVASCRIPT.split("function localCalendarDate", 1)[1].split("function renderState", 1)[0]
        self.assertNotIn("toISOString", helper_source)

    def test_vod_picker_and_selection_controls_have_accessibility_hooks(self) -> None:
        self.assertIn('aria-expanded="false" aria-controls="searchStreamerPickerPanel"', TEMPLATE)
        self.assertIn("function closeSearchStreamerPicker", JAVASCRIPT)
        self.assertIn("function toggleSearchStreamerPicker", JAVASCRIPT)
        self.assertIn("event.key === 'Escape'", JAVASCRIPT)
        self.assertIn('id="searchStreamerFilter" type="search"', TEMPLATE)
        self.assertIn("showVodWorkspaceTab", JAVASCRIPT)
        self.assertIn(".vod-search-workspace", STYLESHEET)
        self.assertIn("Selection stays available while you review results.", TEMPLATE)

    def test_vod_picker_loads_shared_configured_streamers_without_settings_visit(self) -> None:
        picker = _evaluate_search_streamer_picker()

        self.assertIn('value="alpha"', picker["loaded"]["html"])
        self.assertIn('value="beta"', picker["loaded"]["html"])
        self.assertEqual(picker["loaded"]["html"].count('value="alpha"'), 1)
        self.assertEqual(picker["loaded"]["html"].count('value="beta"'), 1)
        self.assertIn('value="beta" checked', picker["loaded"]["html"])
        self.assertNotIn("removed_streamer", picker["loaded"]["html"])
        self.assertEqual(picker["loaded"]["info"], "1 selected")
        self.assertIn('value="alpha"', picker["filtered"]["html"])
        self.assertNotIn('value="beta"', picker["filtered"]["html"])
        self.assertEqual(picker["filtered"]["selected"], ["beta"])
        self.assertEqual(picker["cleared"]["selected"], [])
        self.assertEqual(picker["cleared"]["stored"], "[]")
        self.assertEqual(picker["selectedAll"]["selected"], ["alpha", "beta"])
        self.assertEqual(picker["selectedAll"]["stored"], '["alpha","beta"]')
        self.assertIn("Loading configured streamers", picker["loading"]["html"])
        self.assertIn("Unable to load configured streamers", picker["failed"]["html"])
        self.assertIn("No configured streamers", picker["empty"]["html"])
        self.assertIn("ensureSearchStreamerPickerStreamers", JAVASCRIPT)
        self.assertIn("if (name === 'search') ensureSearchStreamerPickerStreamers();", JAVASCRIPT)

    def test_vod_picker_filter_uses_canonical_selection_and_compact_checkbox_rows(self) -> None:
        picker_renderer = JAVASCRIPT.split("function selectedSearchStreamersFromState", 1)[1].split("function closeSearchStreamerPicker", 1)[0]
        self.assertIn("visibleStreamers", picker_renderer)
        self.assertIn("searchStreamerSelection", picker_renderer)
        self.assertIn("configuredSearchStreamers()", picker_renderer)
        self.assertNotIn("streamer-toggle-pill", picker_renderer)
        self.assertIn(".streamer-picker-option", STYLESHEET)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", STYLESHEET)
        self.assertIn(".streamer-picker-panel .streamer-checkbox-grid { grid-template-columns:1fr;", STYLESHEET)

    def test_vod_picker_close_honors_hidden_state_and_returns_focus(self) -> None:
        result = _evaluate_search_streamer_picker_close()

        self.assertEqual(result["closed"], {"hidden": True, "expanded": "false", "focused": True})
        self.assertEqual(result["reopened"], {"hidden": False, "expanded": "true", "focused": "filter"})
        self.assertIn(".streamer-picker-panel[hidden] { display:none; }", STYLESHEET)
        self.assertIn("closeSearchStreamerPicker({returnFocus:true})", JAVASCRIPT)

    def test_find_vods_mobile_polish_has_compact_rows_and_bounded_picker(self) -> None:
        self.assertIn('id="resolvedDateRange"', TEMPLATE)
        self.assertIn('id="closeSearchStreamerPicker"', TEMPLATE)
        self.assertIn('class="vod-result-row"', JAVASCRIPT)
        for class_name in (
            "vod-result-select",
            "vod-result-streamer",
            "vod-result-date",
            "vod-result-title",
            "vod-result-status",
            "vod-result-link",
        ):
            self.assertIn(class_name, JAVASCRIPT)
        self.assertIn("summary.hidden = custom;", JAVASCRIPT)
        self.assertIn("clear.classList.toggle('hidden', count === 0);", JAVASCRIPT)
        self.assertIn("closeSearchStreamerPicker({returnFocus:true})", JAVASCRIPT)
        self.assertIn("max-height:58vh", STYLESHEET)
        self.assertIn("overflow-y:auto", STYLESHEET)
        self.assertIn("grid-template-areas:", STYLESHEET)
        self.assertIn(".vod-result-title { grid-area:title; display:-webkit-box;", STYLESHEET)
        self.assertIn("td[data-label]::before { display:none; }", STYLESHEET)
        self.assertIn(".vod-custom-dates:not(.is-custom) { display:none; }", STYLESHEET)

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

    def test_local_vods_workspace_owns_upload_controls_and_no_manual_move(self) -> None:
        local_panel = TEMPLATE.split('id="localVodsPanel"', 1)[1].split('id="page-queue"', 1)[0]
        self.assertIn('id="workspacePending" class="heading-count"', TEMPLATE)
        self.assertIn('id="localVodsFilter"', local_panel)
        self.assertIn('id="localSelectionActions"', local_panel)
        self.assertIn('id="uploadSelectedLocalVideos"', local_panel)
        self.assertIn('id="includeUploadedLocalVideos"', local_panel)
        self.assertIn('id="localVideosError"', local_panel)
        self.assertNotIn('id="workspaceTotal"', TEMPLATE)
        self.assertNotIn('id="workspaceSize"', TEMPLATE)
        self.assertNotIn("Move to Uploaded Archive", JAVASCRIPT)
        self.assertIn("Delete the local VOD file and its sidecars", JAVASCRIPT)
        self.assertEqual(TEMPLATE.count('id="localVideoCards"'), 1)
        self.assertEqual(TEMPLATE.count('id="uploadSelectedLocalVideos"'), 1)
        self.assertIn("actions.hidden = selected === 0", JAVASCRIPT)
        self.assertIn("Upload ${selected} VOD", JAVASCRIPT)

    def test_local_vods_tab_loads_the_shared_media_loader_without_queue_visit(self) -> None:
        self.assertIn("if (!find && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});", JAVASCRIPT)
        self.assertIn("if (name === 'search' && !$('localVodsPanel')?.hidden && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});", JAVASCRIPT)
        self.assertNotIn("name === 'queue' && typeof loadLocalVideos", JAVASCRIPT)
        self.assertIn("/api/local-videos?include_uploaded=", JAVASCRIPT)
        self.assertIn("localVideoCache = new Map", JAVASCRIPT)

    def test_local_vod_filtering_preserves_manual_and_automatic_ownership(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Local VOD UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function workspaceStatusClass');
const end = source.indexOf('async function loadLocalVideos', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Local VOD renderer source not found');
const UPLOADED_HISTORY_PAGE_SIZE = 20;
function escapeHtml(value) { return String(value || ''); }
function formatRemainingDuration(value) { return `${value} sec remaining`; }
function streamerAvatarForKnownIdentity(value, size) { return value ? `<avatar data-size="${size}">${value}</avatar>` : ''; }
eval(source.slice(start, end));
const manual = {path:'manual', local_file_exists:true, already_uploaded:false};
const automatic = {path:'automatic', local_file_exists:true, auto_youtube_managed:true};
const cleanup = {path:'cleanup', local_file_exists:true, auto_youtube_managed:true, auto_youtube_cleanup:{state:'scheduled'}};
const attention = {path:'attention', local_file_exists:true, auto_youtube_managed:true, auto_youtube_cleanup:{state:'needs_attention'}};
const uploaded = {path:'uploaded', local_file_exists:false, already_uploaded:true};
const videos = [manual, automatic, cleanup, attention, uploaded];
process.stdout.write(JSON.stringify({
  states:videos.map(localVideoFilterState),
  ready:localVideoRowsForView(videos, true, 'ready').map(item => item.path),
  automatic:localVideoRowsForView(videos, true, 'automatic').map(item => item.path),
  cleanup:localVideoRowsForView(videos, true, 'cleanup').map(item => item.path),
  attention:localVideoRowsForView(videos, true, 'attention').map(item => item.path),
  card:renderLocalVideoCard(automatic)
}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered["states"], ["ready", "automatic", "cleanup", "attention", "uploaded"])
        self.assertEqual(rendered["ready"], ["manual"])
        self.assertEqual(rendered["automatic"], ["automatic"])
        self.assertEqual(rendered["cleanup"], ["cleanup"])
        self.assertEqual(rendered["attention"], ["attention"])
        self.assertIn(">Automatic<", rendered["card"])
        self.assertNotIn('data-action="upload"', rendered["card"])

    def test_local_vods_mobile_workspace_has_compact_structural_hooks(self) -> None:
        self.assertIn(".local-vods-media-workspace", STYLESHEET)
        self.assertIn(".local-selection-actions[hidden]", STYLESHEET)
        self.assertIn(".local-vods-toolbar", STYLESHEET)
        self.assertIn(".video-workspace-card .video-workflow", STYLESHEET)
        self.assertIn("@media (max-width:430px)", STYLESHEET)
        self.assertIn("Local media could not be loaded. Refresh to try again.", JAVASCRIPT)
        self.assertIn("No local VODs are ready for manual upload.", JAVASCRIPT)

    def test_empty_local_vods_hides_bulk_selection_control(self) -> None:
        if not NODE:
            self.skipTest("Node.js is required for Local VOD UI tests")
        runner = r"""
const fs = require('fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function updateLocalBulkSelectionControl');
const end = source.indexOf('function workspaceStatusClass', start);
if (start < 0 || end < 0 || end <= start) throw new Error('Local VOD bulk selection helper not found');
const control = {hidden:false, disabled:false};
function $(id) { return id === 'checkAllLocalVideos' ? control : null; }
eval(source.slice(start, end));
updateLocalBulkSelectionControl(false);
const empty = {hidden:control.hidden, disabled:control.disabled};
updateLocalBulkSelectionControl(true);
const ready = {hidden:control.hidden, disabled:control.disabled};
process.stdout.write(JSON.stringify({empty, ready}));
"""
        completed = subprocess.run([NODE, "-e", runner], cwd=ROOT, encoding="utf-8", capture_output=True, check=False)
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered["empty"], {"hidden": True, "disabled": True})
        self.assertEqual(rendered["ready"], {"hidden": False, "disabled": False})

    def test_prepare_metadata_is_secondary_under_actions(self) -> None:
        self.assertNotIn('id="prepareSelectedLocalVideos"', TEMPLATE)
        local_card = JAVASCRIPT.split("function renderLocalVideoCard", 1)[1].split("function visibleLocalVideoRows", 1)[0]
        self.assertNotIn("More actions", local_card)
        self.assertIn("<summary>Actions</summary>", local_card)
        self.assertIn(">Prepare metadata</button>", local_card)
        self.assertNotIn(">Prepare</button>", local_card)

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

    def test_auto_youtube_owned_vod_has_status_without_manual_upload_controls(self) -> None:
        owned = {
            "path": "C:/media/cptmary/owned.mp4",
            "name": "owned.mp4",
            "streamer": "cptmary",
            "date_de": "27.08.2026",
            "title": "[Peak-RP] It's Sassy Toni",
            "size_gb": 12.35,
            "prepared": False,
            "already_uploaded": False,
            "local_file_exists": True,
            "auto_youtube_managed": True,
            "auto_youtube_video_confirmed": False,
            "auto_youtube_status": "Managed by Auto YouTube",
        }

        html = _evaluate_local_history_ui(owned, [owned])["card"]

        self.assertIn("Managed by Auto YouTube", html)
        self.assertIn("Automatic upload lifecycle", html)
        self.assertIn(">Automatic<", html)
        self.assertNotIn("localvideocheck", html)
        self.assertNotIn('data-action="upload"', html)
        self.assertNotIn(">Prepare metadata<", html)
        self.assertNotIn(">Mark as Uploaded<", html)
        self.assertIn('data-action="delete"', html)

    def test_completed_auto_youtube_cleanup_status_and_keep_action_are_scoped(self) -> None:
        owned = {
            "path": "C:/media/cptmary/owned.mp4", "name": "owned.mp4",
            "streamer": "cptmary", "date_de": "29.08.2026", "title": "Canary",
            "size_gb": 12.35, "prepared": False, "already_uploaded": False,
            "local_file_exists": True, "auto_youtube_managed": True,
            "auto_youtube_video_confirmed": True,
            "auto_youtube_status": "Uploaded by Auto YouTube",
            "auto_youtube_streamer": "cptmary",
            "auto_youtube_twitch_vod_id": "2855270041",
            "auto_youtube_cleanup": {
                "state": "scheduled", "cleanup_due_at": "2099-08-29T18:00:00Z",
                "can_keep_local": True, "can_resume_cleanup": False,
            },
        }
        html = _evaluate_local_history_ui(owned, [owned])["card"]
        self.assertIn("Uploaded by Auto YouTube", html)
        self.assertIn("Local cleanup in", html)
        self.assertIn('data-action="keep-local"', html)
        self.assertNotIn('data-action="upload"', html)

        manual = dict(owned, auto_youtube_managed=False, auto_youtube_cleanup=None)
        manual_html = _evaluate_local_history_ui(manual, [manual])["card"]
        self.assertNotIn("Keep local", manual_html)
        self.assertNotIn("Local cleanup", manual_html)

    def test_auto_youtube_queue_cleanup_states_remain_visible_after_media_removal(self) -> None:
        expected = {
            "scheduled": "Local cleanup scheduled",
            "cleaning": "Removing local copy",
            "removed": "Local copy removed",
            "needs_attention": "Local cleanup needs attention",
        }
        for state, label in expected.items():
            with self.subTest(state=state):
                item = {
                    "index": 0, "itemId": "upload-1-item-1",
                    "state": "completed", "streamer": "cptmary",
                    "date": "29.08.2026", "title": "Uploaded VOD",
                    "operation": "YouTube upload", "capabilities": {},
                    "resolved": False, "error": "", "progress": 100,
                    "extra": "", "completionReason": "completed",
                    "recoveryReason": "", "failureKind": "",
                    "job": {
                        "id": "upload-1", "type": "youtube_upload",
                        "origin": "auto_youtube", "label": "Auto YouTube",
                        "state": "completed", "item_states": ["completed"],
                        "auto_youtube_cleanup": {
                            "state": state,
                            "reason": "filesystem_error" if state == "needs_attention" else None,
                        },
                    },
                }
                html = _render_queue_item_with_saved_open_state(item)
                self.assertIn(label, html)
                self.assertNotIn("Keep local", html)
                self.assertNotIn("Start upload", html)
                if state == "needs_attention":
                    self.assertIn("Local cleanup reason", html)
                    self.assertIn("filesystem_error", html)

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

        self.assertIn('value="PLAYLIST_A" selected', result["configuredOptions"])
        self.assertIn(">No playlist</option>", result["emptyOptions"])
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
        self.assertIn("streamerWorkspaceEntries(", JAVASCRIPT)
        self.assertIn("'/api/streamers/policy'", JAVASCRIPT)
        self.assertNotIn("withStreamerAutoVodSelection", JAVASCRIPT)
        self.assertNotIn("withStreamerAutoYoutubeSelection", JAVASCRIPT)

    def test_auto_recorder_controls_are_visible_compact_and_accessible(self) -> None:
        self.assertIn('id="autoRecorderEnabled"', TEMPLATE)
        self.assertNotIn('id="autoRecorderEnabled" checked', TEMPLATE)
        self.assertIn('role="switch"', TEMPLATE)
        self.assertIn('aria-label="Run Automatic Live Recording"', TEMPLATE)
        self.assertIn(
            "$('autoRecorderEnabled').checked = state.settings.auto_recorder_enabled === true",
            JAVASCRIPT,
        )
        self.assertIn(
            "auto_recorder_enabled:$('autoRecorderEnabled').checked",
            JAVASCRIPT,
        )
        self.assertIn("Paused · streamer policies are preserved.", TEMPLATE)
        self.assertIn('class="streamer-live-recording-select"', JAVASCRIPT)
        self.assertIn('<option value="manual" ${editorValues.live_recording', JAVASCRIPT)
        self.assertIn('<option value="automatic" ${editorValues.live_recording', JAVASCRIPT)
        self.assertIn("min-height:44px", STYLESHEET)
        self.assertIn("input:focus-visible + .switch-track", STYLESHEET)

    def test_auto_youtube_settings_are_visible_but_not_an_active_workflow(self) -> None:
        self.assertIn('id="autoYoutubeEnabled"', TEMPLATE)
        self.assertNotIn('id="autoYoutubeEnabled" checked', TEMPLATE)
        self.assertIn("Automatic YouTube Processing", TEMPLATE)
        self.assertIn("Paused · Download + YouTube streamer policies are preserved.", JAVASCRIPT)
        self.assertIn(
            "$('autoYoutubeEnabled').checked = state.settings.auto_youtube_enabled === true",
            JAVASCRIPT,
        )
        self.assertIn(
            "auto_youtube_enabled:$('autoYoutubeEnabled').checked",
            JAVASCRIPT,
        )
        self.assertIn('id="autoYoutubeCleanupDelayHours"', TEMPLATE)
        self.assertIn('<option value="0">Keep local copies</option>', TEMPLATE)
        self.assertIn('Remove after 6 hours', TEMPLATE)
        self.assertIn('Global default for new VODs entering the automatic YouTube lifecycle.', TEMPLATE)
        self.assertIn("auto_youtube_cleanup_delay_hours:Number($('autoYoutubeCleanupDelayHours').value || 0)", JAVASCRIPT)
        self.assertIn('class="streamer-vod-handling-select"', JAVASCRIPT)
        self.assertIn('<option value="download_and_youtube" ${editorMode', JAVASCRIPT)
        self.assertNotIn('data-page="auto-youtube"', TEMPLATE)
        self.assertIn(".streamer-editor-row", STYLESHEET)
        self.assertIn(".streamer-policy-fields", STYLESHEET)

    def test_streamer_auto_record_and_playlist_round_trip_independently(self) -> None:
        saver = JAVASCRIPT.split("async function saveStreamerPolicy", 1)[1].split("function updateStreamerEditorButtons", 1)[0]
        self.assertIn("live_recording", saver)
        self.assertIn("youtube_playlist_id", saver)
        self.assertIn("vod_handling", saver)
        self.assertIn("No playlist", JAVASCRIPT)
        self.assertIn("Optional. Blank means no playlist for automatic uploads.", JAVASCRIPT)

    def test_streamer_auto_youtube_and_playlist_round_trip_independently(self) -> None:
        renderer = JAVASCRIPT.split("function renderStreamerEditor", 1)[1].split("async function saveStreamerPolicy", 1)[0]
        self.assertIn("vod_handling", renderer)
        self.assertIn("manual", renderer)
        self.assertIn("auto_download", renderer)
        self.assertIn("download_and_youtube", renderer)
        self.assertIn("Needs Review", renderer)
        self.assertNotIn("auto_vod_download", renderer)
        self.assertNotIn("auto_youtube_upload", renderer)

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
        general = TEMPLATE.split('data-settings-panel="general"', 1)[1].split('data-settings-panel="automation"', 1)[0]
        advanced = TEMPLATE.split('data-settings-panel="advanced"', 1)[1]
        self.assertNotIn("Concurrent Fragments", general)
        self.assertIn("Concurrent fragments", advanced)
        self.assertNotIn("After a Batch", TEMPLATE)
        self.assertIn("Post-processing timing", general)
        self.assertNotIn("When to Prepare or Upload", TEMPLATE)
        self.assertIn("move the local VOD bundle to the uploaded archive", TEMPLATE)
        self.assertIn("Automatic uploads keep their per-streamer playlist ownership.", TEMPLATE)
        self.assertIn("Available template variables", TEMPLATE)
        self.assertIn('id="saveAdvancedSettings"', advanced)
        self.assertNotIn("Save All Settings", TEMPLATE)
        self.assertIn("settings-maintenance-actions", TEMPLATE)
        self.assertIn("YouTube is not connected. Connect your account to enable uploads.", JAVASCRIPT)
        self.assertNotIn("YouTubeNotConnectedError", TEMPLATE + JAVASCRIPT)
        self.assertIn("refreshButton.disabled = !data.connected", JAVASCRIPT)

    def test_settings_tabs_keep_unique_aria_relationships_and_hidden_isolation(self) -> None:
        tabs = re.findall(
            r'<button[^>]+id="(settingsTab[^"]+)"[^>]+data-settings-tab="([^"]+)"[^>]+aria-controls="([^"]+)"',
            TEMPLATE,
        )
        panels = re.findall(
            r'<section[^>]+id="(settingsPanel[^"]+)"[^>]+data-settings-panel="([^"]+)"[^>]+aria-labelledby="([^"]+)"',
            TEMPLATE,
        )
        self.assertEqual([name for _, name, _ in tabs], ["general", "automation", "streamers", "youtube", "advanced"])
        self.assertEqual([name for _, name, _ in panels], ["general", "automation", "streamers", "youtube", "advanced"])
        self.assertEqual(len({tab_id for tab_id, _, _ in tabs}), 5)
        self.assertEqual(len({panel_id for panel_id, _, _ in panels}), 5)
        self.assertEqual([controls for _, _, controls in tabs], [panel_id for panel_id, _, _ in panels])
        self.assertEqual([labelled_by for _, _, labelled_by in panels], [tab_id for tab_id, _, _ in tabs])
        self.assertIn("panel.hidden = !active;", JAVASCRIPT)
        self.assertIn("panel.setAttribute('aria-hidden', active ? 'false' : 'true');", JAVASCRIPT)
        self.assertIn(".settings-panel[hidden] { display:none !important; }", STYLESHEET)

    def test_settings_tab_binding_survives_slice_nine_advanced_save_rename(self) -> None:
        self.assertIn("$('saveAdvancedSettings').addEventListener", JAVASCRIPT)
        self.assertNotIn("$('saveYoutubeSettingsBottom').addEventListener", JAVASCRIPT)
        self.assertIn("const settingsTabs = [...document.querySelectorAll('.settings-tab')];", JAVASCRIPT)
        binding = JAVASCRIPT.split("const settingsTabs = [...document.querySelectorAll('.settings-tab')];", 1)[1].split("let initialTab", 1)[0]
        self.assertIn("tab.addEventListener('click', () => showSettingsTab", binding)
        self.assertIn("ArrowLeft", binding)
        self.assertIn("ArrowRight", binding)
        self.assertIn("Home", binding)
        self.assertIn("End", binding)

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
