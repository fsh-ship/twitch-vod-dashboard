const VOD_CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || '';

function showToast(message, kind='good') {
  const toast = document.getElementById('appToast');
  if (!toast) return;
  toast.textContent = message;
  toast.className = 'app-toast ' + kind;
  clearTimeout(window.__vodToastTimer);
  window.__vodToastTimer = setTimeout(() => {
    toast.className = 'app-toast hidden';
  }, 3200);
}

async function copyTextToClipboard(text, label='Text') {
  const value = String(text || '');
  if (!value) throw new Error(`${label} is empty.`);
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
  } else {
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  showToast(`${label} copied.`);
}

function currentPageName() {
  if (document.body.dataset.activePage) return document.body.dataset.activePage;
  const active = document.querySelector('.page.active');
  return active ? active.id.replace('page-', '') : '';
}



function setSingleVodStatus(message, kind='muted') {
  const el = document.getElementById('singleVodStatus');
  if (!el) return;
  el.textContent = message;
  el.className = 'inline-status ' + kind;
}

function normalizeVodInputClient(raw) {
  let s = (raw || '').trim().replace(/^["']|["']$/g, '');
  if (!s) return '';
  if (/^\d{6,}$/.test(s)) return 'https://www.twitch.tv/videos/' + s;
  if (s.startsWith('www.')) s = 'https://' + s;
  if (s.startsWith('twitch.tv/')) s = 'https://www.' + s;
  s = s.replace('https://m.twitch.tv/', 'https://www.twitch.tv/');
  const m = s.match(/(?:videos\/|video=|v=)(\d{6,})/);
  if (m) return 'https://www.twitch.tv/videos/' + m[1];
  return s;
}

async function validateSingleVodLink(showAlert=false) {
  const input = $('singleUrl');
  if (!input) {
    setSingleVodStatus('VOD input field not found.', 'bad');
    return null;
  }
  input.value = normalizeVodInputClient(input.value);
  setSingleVodStatus('Validating VOD link...', 'muted');
  const data = await api('/api/vod/validate', { method:'POST', body: JSON.stringify({ url: input.value }) });
  if (!data.ok) {
    setSingleVodStatus(data.error || 'Invalid Twitch VOD link.', 'bad');
    if (showAlert) alert(data.error || 'Invalid Twitch VOD link.');
    return null;
  }
  input.value = data.url;
  setSingleVodStatus('Valid VOD link: ' + data.vod_id, 'good');
  return data;
}

async function startSingleVodDownload() {
  const checked = await validateSingleVodLink(false);
  if (!checked) return;
  setSingleVodStatus('Creating download job...', 'muted');
  const data = await api('/api/download', { method:'POST', body: JSON.stringify({ url: checked.url, label:'Single VOD ' + checked.vod_id }) });
  setSingleVodStatus('VOD added to the download queue.', 'good');
  await pollJobs();
  return data;
}



(function robustSettingsSaveBootstrap() {
  function byId(id) { return document.getElementById(id); }
  function val(id, fallback) {
    const el = byId(id);
    if (!el) return fallback || '';
    if (el.type === 'checkbox') return !!el.checked;
    return el.value;
  }
  function setText(id, text) {
    const el = byId(id);
    if (el) el.textContent = text;
  }
  function collectSettingsFallback() {
    return {
      download_path: val('downloadPath'),
      streamer_file: val('streamerFile'),
      archive_file: val('archiveFile'),
      cookie_browser: val('cookieBrowser'),
      quality: val('quality', 'source/best'),
      fragments: val('fragments', '8'),
      twitch_rate_limit: val('twitchRateLimit'),
      batch_postprocess_mode: val('batchPostprocessMode', 'after_each'),
      merge_format: val('mergeFormat', 'mp4'),
      include_unknown_dates: val('includeUnknownDates', true),
      exclude_live_streams: val('excludeLiveStreams', true),
      only_real_vod_urls: val('onlyRealVodUrls', true),
      strict_date_filter: val('strictDateFilter', false),
      output_template: val('outputTemplate'),
      playlist_end: val('limit', '150'),
      auto_recorder_enabled: val('autoRecorderEnabled', false),
      youtube_enabled: val('youtubeEnabled', false),
      youtube_auto_upload: val('youtubeAutoUpload', false),
      move_uploaded_vods: val('moveUploadedVods', true),
      uploaded_vods_folder: val('uploadedVodsFolder'),
      youtube_privacy_status: val('youtubePrivacyStatus', 'private'),
      youtube_playlist_id: val('youtubePlaylistId'),
      youtube_client_secret_file: val('youtubeClientSecretFile'),
      youtube_token_file: val('youtubeTokenFile'),
      youtube_description: val('youtubeDescription'),
      youtube_tags: val('youtubeTags'),
      youtube_category_id: val('youtubeCategoryId', '20'),
      youtube_title_template: val('youtubeTitleTemplate'),
      youtube_description_template: val('youtubeDescriptionTemplate'),
      youtube_chunk_size_mb: val('youtubeChunkSizeMb', '64'),
      youtube_upload_mode: val('youtubeUploadMode', 'stable'),
      manual_upload_filename_template: val('manualUploadFilenameTemplate'),
      manual_upload_prepare_enabled: val('manualUploadPrepareEnabled', true),
      manual_upload_rename_video: val('manualUploadRenameVideo', true),
      manual_upload_write_description: val('manualUploadWriteDescription', true),
      manual_upload_write_metadata_json: val('manualUploadWriteMetadataJson', true)
    };
  }

  async function postJson(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type':'application/json',
        'X-CSRF-Token': VOD_CSRF_TOKEN
      },
      body: JSON.stringify(payload || {})
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {error:text}; }
    if (!res.ok) throw new Error(data.error || text || ('HTTP ' + res.status));
    return data;
  }

  window.vodRobustSaveSettings = async function(ev, scope) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const btn = ev && ev.target ? ev.target.closest('button') : null;
    const oldText = btn ? btn.textContent : '';
    try {
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving...';
      }
      setText('settingsSaveStatus', 'saving...');
      const saved = await postJson('/api/settings', collectSettingsFallback());
      if (saved._settings_file) setText('settingsFilePath', saved._settings_file);
      setText('settingsSaveStatus', 'saved: ' + (saved._saved_at || new Date().toLocaleTimeString()));
      if (typeof window.loadState === 'function') {
        try { await window.loadState(); } catch(e) {}
      }
      if (typeof window.refreshDashboard === 'function') {
        try { await window.refreshDashboard(); } catch(e) {}
      }
      if (typeof window.refreshAutoRecorderStatus === 'function') {
        try { await window.refreshAutoRecorderStatus(); } catch(e) {}
      }
      alert((scope === 'youtube' ? 'YouTube settings' : 'Settings') + ' saved.\n\nFile: ' + (saved._settings_file || 'unknown'));
      return saved;
    } catch (e) {
      console.error(e);
      const autoRecorder = byId('autoRecorderEnabled');
      if (autoRecorder && typeof state !== 'undefined' && state?.settings) {
        autoRecorder.checked = state.settings.auto_recorder_enabled === true;
        if (typeof updateAutoRecorderSettingCopy === 'function') updateAutoRecorderSettingCopy();
      }
      setText('settingsSaveStatus', 'Error: ' + e.message);
      alert('Save failed:\n\n' + e.message);
      throw e;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = oldText || 'Save';
      }
    }
  };

  window.vodCheckSettingsStatus = async function(ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    try {
      const res = await fetch('/api/settings/status');
      const data = await res.json();
      setText('settingsFilePath', data.settings_file || 'unknown');
      setText('settingsSaveStatus', data.can_write_settings_folder ? 'Settings folder is writable' : ('Not writable: ' + (data.write_error || 'unknown')));
      alert(
        'Settings file:\n' + (data.settings_file || 'unknown') +
        '\n\nWritable: ' + (data.can_write_settings_folder ? 'yes' : 'no') +
        '\n\nDownload path:\n' + (data.download_path || '')
      );
    } catch (e) {
      setText('settingsSaveStatus', 'Status error: ' + e.message);
      alert('Status check failed:\n\n' + e.message);
    }
  };

  document.addEventListener('click', function(ev) {
    const btn = ev.target.closest && ev.target.closest('#saveSettings, #saveYoutubeSettings, #saveYoutubeSettingsBottom');
    if (btn) {
      window.vodRobustSaveSettings(ev, btn.id === 'saveSettings' ? 'settings' : 'youtube');
    }
  }, true);
})();


(function bootstrapNavigationEarly() {
  function byId(id) { return document.getElementById(id); }
  const YTDLP_DEFAULT_OUTPUT_TEMPLATE = '%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s';
const MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = '{date_de} - {streamer} - {title}';

const pageMetaEarly = {
    dashboard: ['Dashboard', 'What needs attention and what is happening now.'],
    search: ['Search VODs', 'Choose a date range and streamers, then select VODs to download.'],
    queue: ['Queue', 'Follow individual VODs from download through YouTube upload.'],
    settings: ['Settings', 'Manage downloads, streamers, YouTube, and advanced options.']
  };
  window.vodShowPage = function(name) {
    if (!pageMetaEarly[name]) name = 'dashboard';
    document.querySelectorAll('.page').forEach(function(page) {
      page.classList.toggle('active', page.id === 'page-' + name);
    });
    document.querySelectorAll('.nav-btn').forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.page === name);
    });
    const meta = pageMetaEarly[name];
    if (byId('pageTitle')) byId('pageTitle').textContent = meta[0];
    if (byId('pageSubtitle')) byId('pageSubtitle').textContent = meta[1];
    try { localStorage.setItem('vodActivePage', name); } catch (e) {}
  };
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.nav-btn, .goto-page').forEach(function(btn) {
      btn.addEventListener('click', function(ev) {
        ev.preventDefault();
        window.vodShowPage(btn.dataset.page || 'dashboard');
      });
    });
    let initial = 'dashboard';
    try { initial = localStorage.getItem('vodActivePage') || 'dashboard'; } catch (e) {}
    window.vodShowPage(initial);
  });
})();

let state = null;
let lastResults = [];
let jobOpenState = {};
let queueDetailOpenState = {};
let autoExpandJobDetails = localStorage.getItem('vodJobAutoExpand') === '1';
let youtubePlaylistChoices = [];
let streamerProfileDraft = {};
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

const LIVE_STATUS_CONCURRENCY = 2;
const AUTO_RECORDER_STATUS_REFRESH_MS = 15000;
const ACTIVE_RECORDING_STATES = new Set(['queued', 'running', 'stopping']);

const $ = (id) => document.getElementById(id);

const YTDLP_DEFAULT_OUTPUT_TEMPLATE = '%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s';
const MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = '{date_de} - {streamer} - {title}';

const pageMeta = {
  dashboard: ['Dashboard', 'What needs attention and what is happening now.'],
  search: ['Search VODs', 'Choose a date range and streamers, then select VODs to download.'],
  queue: ['Queue', 'Follow individual VODs from download through YouTube upload.'],
  settings: ['Settings', 'Manage downloads, streamers, YouTube, and advanced options.']
};

function showPage(name) {
  if (!pageMeta[name]) name = 'dashboard';
  document.body.dataset.activePage = name;
  const selectionBar = document.getElementById('searchSelectionBar');
  if (selectionBar && name !== 'search') {
    selectionBar.classList.add('hidden');
    selectionBar.style.display = 'none';
    selectionBar.setAttribute('aria-hidden', 'true');
  }
  window.vodShowPage(name);
  refreshDashboard().catch(() => {});
  if (name === 'queue' && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});
  if (typeof refreshSelectionState === 'function') refreshSelectionState();
}


async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers['X-CSRF-Token'] = VOD_CSRF_TOKEN;
  const res = await fetch(path, { ...options, headers });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text.slice(0, 1000) }; }
  if (!res.ok) {
    const detail = [data.error || `HTTP ${res.status}`, data.hint, data.client_secret_path ? ('client_secret: ' + data.client_secret_path) : '', data.token_path ? ('token: ' + data.token_path) : ''].filter(Boolean).join('\n\n');
    const error = new Error(detail);
    error.reason = String(data.reason || '');
    error.status = res.status;
    throw error;
  }
  return data;
}

function setDateRange(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - days + 1);
  $('fromDate').value = start.toISOString().slice(0,10);
  $('toDate').value = end.toISOString().slice(0,10);
}

function renderState() {
  $('archiveCount').textContent = `Archive: ${state.archive_count} VODs`;
  if ($('settingsFilePath')) $('settingsFilePath').textContent = state.settings_file || state.settings._settings_file || 'unknown';
  streamerProfileDraft = cloneStreamerProfiles(state.settings.streamer_profiles);
  $('streamersText').value = state.streamers.join('\n');
  renderStreamerEditor();
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = state.streamer_file_resolved || state.settings.streamer_file || 'unknown';
  if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${state.streamers.length} streamers loaded`;
  $('limit').value = state.settings.playlist_end;
  $('downloadPath').value = state.settings.download_path;
  $('streamerFile').value = state.settings.streamer_file;
  $('archiveFile').value = state.settings.archive_file;
  $('strictDateFilter').checked = !!state.settings.strict_date_filter;
  $('cookieBrowser').value = state.settings.cookie_browser;
  $('quality').value = state.settings.quality;
  $('fragments').value = state.settings.fragments;
  $('twitchRateLimit').value = state.settings.twitch_rate_limit || '';
  $('batchPostprocessMode').value = state.settings.batch_postprocess_mode || 'after_each';
  $('mergeFormat').value = state.settings.merge_format;
  $('includeUnknownDates').checked = !!state.settings.include_unknown_dates;
  $('excludeLiveStreams').checked = state.settings.exclude_live_streams !== false;
  $('onlyRealVodUrls').checked = state.settings.only_real_vod_urls !== false;
  $('outputTemplate').value = state.settings.output_template;
  $('autoRecorderEnabled').checked = state.settings.auto_recorder_enabled === true;
  updateAutoRecorderSettingCopy();
  $('youtubeEnabled').checked = !!state.settings.youtube_enabled;
  $('youtubeAutoUpload').checked = !!state.settings.youtube_auto_upload;
  $('moveUploadedVods').checked = state.settings.move_uploaded_vods !== false;
  $('uploadedVodsFolder').value = state.settings.uploaded_vods_folder || '';
  $('youtubePrivacyStatus').value = state.settings.youtube_privacy_status || 'private';
  $('youtubeClientSecretFile').value = state.settings.youtube_client_secret_file || '';
  $('youtubeTokenFile').value = state.settings.youtube_token_file || '';
  $('youtubeDescription').value = state.settings.youtube_description || '';
  $('youtubeTags').value = state.settings.youtube_tags || '';
  setYoutubeCategoryValue(state.settings.youtube_category_id || '20');
  $('youtubeTitleTemplate').value = state.settings.youtube_title_template || '{streamer} VOD - {date_de} - {title}';
  $('youtubeDescriptionTemplate').value = state.settings.youtube_description_template || '';
  $('manualUploadFilenameTemplate').value = state.settings.manual_upload_filename_template || MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE;
  $('manualUploadPrepareEnabled').checked = state.settings.manual_upload_prepare_enabled !== false;
  $('manualUploadRenameVideo').checked = state.settings.manual_upload_rename_video !== false;
  $('manualUploadWriteDescription').checked = state.settings.manual_upload_write_description !== false;
  $('manualUploadWriteMetadataJson').checked = state.settings.manual_upload_write_metadata_json !== false;
  $('youtubeChunkSizeMb').value = String(state.settings.youtube_chunk_size_mb || 64);
  $('youtubeUploadMode').value = state.settings.youtube_upload_mode || 'stable';
  renderGlobalPlaylistSelect();
  renderLocalUploadPlaylistSelect();
  $('singleStreamer').innerHTML = state.streamers.map(s => `<option>${escapeHtml(s)}</option>`).join('');
  renderSearchStreamerCheckboxes();
  syncLiveStreamers(state.streamers);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

async function loadState() {
  state = await api('/api/state');
  renderState();
  initializeLiveStatuses();
}
window.loadState = loadState;


function storedSearchStreamerSelection() {
  try {
    const raw = localStorage.getItem('vodSearchStreamerSelection');
    const data = raw ? JSON.parse(raw) : null;
    return Array.isArray(data) ? data : [];
  } catch {
    return [];
  }
}

function saveSearchStreamerSelection(names) {
  try { localStorage.setItem('vodSearchStreamerSelection', JSON.stringify(names || [])); } catch {}
}

function renderSearchStreamerCheckboxes() {
  const box = $('searchStreamerCheckboxes');
  const info = $('searchStreamerToggleInfo');
  if (!box || !state || !state.streamers) return;

  const saved = storedSearchStreamerSelection();
  const valid = new Set(state.streamers);
  let selected = saved.filter(s => valid.has(s));
  if (!saved.length) selected = [...state.streamers];
  const selectedSet = new Set(selected);

  if (!state.streamers.length) {
    box.innerHTML = '<div class="muted">No streamers loaded.</div>';
    if (info) info.textContent = '0 selected';
    return;
  }

  box.innerHTML = state.streamers.map(s => `
    <label class="streamer-toggle-pill">
      <input type="checkbox" class="search-streamer-check" value="${escapeHtml(s)}" ${selectedSet.has(s) ? 'checked' : ''}>
      <span>${escapeHtml(s)}</span>
    </label>
  `).join('');

  document.querySelectorAll('.search-streamer-check').forEach(cb => {
    cb.addEventListener('change', () => {
      const selectedNow = selectedSearchStreamersFromCheckboxes();
      saveSearchStreamerSelection(selectedNow);
      updateSearchStreamerToggleInfo();
    });
  });
  updateSearchStreamerToggleInfo();
}

function selectedSearchStreamersFromCheckboxes() {
  return [...document.querySelectorAll('.search-streamer-check:checked')].map(cb => cb.value);
}

function updateSearchStreamerToggleInfo() {
  const info = $('searchStreamerToggleInfo');
  if (!info) return;
  const selected = selectedSearchStreamersFromCheckboxes();
  const total = state && state.streamers ? state.streamers.length : 0;
  info.textContent = `${selected.length}/${total} selected`;
}

function setAllSearchStreamers(checked) {
  document.querySelectorAll('.search-streamer-check').forEach(cb => cb.checked = checked);
  saveSearchStreamerSelection(selectedSearchStreamersFromCheckboxes());
  updateSearchStreamerToggleInfo();
}

function selectedSearchStreamersForSearch() {
  const mode = $('streamerMode').value;
  if (mode === 'one') return [$('singleStreamer').value].filter(Boolean);
  if (mode === 'all') return state.streamers || [];
  return selectedSearchStreamersFromCheckboxes();
}


function selectedUrls() {
  return [...document.querySelectorAll('.rowcheck:checked')].map(cb => cb.dataset.url);
}

function refreshSelectionState() {
  const count = selectedUrls().length;
  const btn = $('downloadSelected');
  if (btn) {
    btn.disabled = count === 0;
    btn.textContent = count ? `Download ${count} Selected` : 'Download Selected';
  }

  const bar = $('searchSelectionBar');
  const stickyBtn = $('stickyDownloadSelected');
  const stickyCount = $('searchSelectionBarCount');
  const page = currentPageName();
  const visible = count > 0 && page === 'search';

  if (bar) {
    bar.classList.toggle('hidden', !visible);
    bar.setAttribute('aria-hidden', visible ? 'false' : 'true');
    bar.style.display = visible ? 'flex' : 'none';
  }
  if (stickyBtn) {
    stickyBtn.disabled = count === 0;
    stickyBtn.textContent = count ? `Download ${count} VODs` : 'Download Selected';
  }
  if (stickyCount) stickyCount.textContent = `${count} VOD${count === 1 ? '' : 's'} selected`;
}

function setStreamerSelection(streamer, checked) {
  document.querySelectorAll('.rowcheck').forEach(cb => {
    if (cb.dataset.streamer === streamer) cb.checked = checked;
  });
  refreshSelectionState();
}

function selectNewResults() {
  document.querySelectorAll('.rowcheck').forEach(cb => {
    cb.checked = cb.dataset.alreadyDownloaded !== 'true';
  });
  refreshSelectionState();
}

function clearResultsSelection() {
  document.querySelectorAll('.rowcheck').forEach(cb => cb.checked = false);
  const all = $('checkAll');
  if (all) all.checked = false;
  refreshSelectionState();
}

function selectedResultObjects() {
  const urls = new Set(selectedUrls());
  return lastResults.filter(r => urls.has(r.url));
}

function setYoutubeCategoryValue(value) {
  const select = $('youtubeCategoryId');
  if (!select) return;
  const categoryId = String(value || '20');
  if (![...select.options].some(option => option.value === categoryId)) {
    const option = document.createElement('option');
    option.value = categoryId;
    option.textContent = `Category ID ${categoryId}`;
    select.appendChild(option);
  }
  select.value = categoryId;
}

function canonicalStreamerLoginClient(value) {
  const login = String(value || '').trim().replace(/^@+/, '').toLowerCase();
  return /^[a-z0-9_]{1,25}$/.test(login) ? login : '';
}

function cloneStreamerProfiles(profiles) {
  const normalized = {};
  Object.entries(profiles || {}).forEach(([rawLogin, profile]) => {
    const login = canonicalStreamerLoginClient(rawLogin);
    const playlistId = String(profile?.youtube_playlist_id || '').trim();
    const normalizedProfile = {};
    if (playlistId) normalizedProfile.youtube_playlist_id = playlistId;
    if (profile?.auto_record === true) normalizedProfile.auto_record = true;
    if (profile?.auto_vod_download === true) normalizedProfile.auto_vod_download = true;
    if (login && Object.keys(normalizedProfile).length && !normalized[login]) {
      normalized[login] = normalizedProfile;
    }
  });
  return normalized;
}

function streamerProfilePlaylistId(profiles, streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  if (!login) return '';
  return String(profiles?.[login]?.youtube_playlist_id || '').trim();
}

function streamerProfileAutoRecordEnabled(profiles, streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  return !!login && profiles?.[login]?.auto_record === true;
}

function withStreamerPlaylistSelection(profiles, streamer, playlistId) {
  const updated = cloneStreamerProfiles(profiles);
  const login = canonicalStreamerLoginClient(streamer);
  if (!login) return updated;
  const selected = String(playlistId || '').trim();
  const profile = { ...(updated[login] || {}) };
  if (selected) profile.youtube_playlist_id = selected;
  else delete profile.youtube_playlist_id;
  if (Object.keys(profile).length) updated[login] = profile;
  else delete updated[login];
  return updated;
}

function withStreamerAutoRecordSelection(profiles, streamer, enabled) {
  const updated = cloneStreamerProfiles(profiles);
  const login = canonicalStreamerLoginClient(streamer);
  if (!login) return updated;
  const profile = { ...(updated[login] || {}) };
  if (enabled === true) profile.auto_record = true;
  else delete profile.auto_record;
  if (Object.keys(profile).length) updated[login] = profile;
  else delete updated[login];
  return updated;
}

function buildYoutubeUploadRequest(paths, mode, playlistId='') {
  const payload = { paths:[...(paths || [])] };
  if (mode === 'streamer-default') return payload;
  payload.playlist_id = mode === 'no-playlist'
    ? ''
    : String(playlistId || '').trim();
  return payload;
}

function availablePlaylistChoices(currentId='') {
  const choices = (youtubePlaylistChoices || []).map(playlist => ({
    id: String(playlist.id || '').trim(),
    title: String(playlist.title || playlist.id || '').trim()
  })).filter(playlist => playlist.id);
  const current = String(currentId || '').trim();
  if (current && !choices.some(playlist => playlist.id === current)) {
    choices.unshift({ id:current, title:`Saved playlist (${current})` });
  }
  return choices;
}

function playlistOptionsHtml(currentId, emptyLabel) {
  const current = String(currentId || '').trim();
  return `<option value="" ${current ? '' : 'selected'}>${escapeHtml(emptyLabel)}</option>` + availablePlaylistChoices(current).map(playlist => `<option value="${escapeHtml(playlist.id)}" ${playlist.id === current ? 'selected' : ''}>${escapeHtml(playlist.title)}</option>`).join('');
}

function renderGlobalPlaylistSelect() {
  const select = $('youtubePlaylistId');
  if (!select || !state?.settings) return;
  const current = String(state.settings.youtube_playlist_id || '').trim();
  select.innerHTML = playlistOptionsHtml(current, 'No Playlist');
}

function renderLocalUploadPlaylistSelect() {
  const select = $('localUploadPlaylistId');
  if (!select) return;
  const selected = select.selectedOptions?.[0];
  const previousMode = selected?.dataset.playlistMode || 'streamer-default';
  const previousId = selected?.value || '';
  const concreteChoices = availablePlaylistChoices(
    previousMode === 'playlist' ? previousId : ''
  );
  select.innerHTML = '<option value="" data-playlist-mode="streamer-default">Streamer Default</option><option value="" data-playlist-mode="no-playlist">No Playlist</option>' + concreteChoices.map(playlist => `<option value="${escapeHtml(playlist.id)}" data-playlist-mode="playlist">${escapeHtml(playlist.title)}</option>`).join('');
  const match = [...select.options].find(option => (
    option.dataset.playlistMode === previousMode
    && (previousMode !== 'playlist' || option.value === previousId)
  ));
  (match || select.options[0]).selected = true;
}

function localUploadRequestPayload(paths) {
  const selected = $('localUploadPlaylistId')?.selectedOptions?.[0];
  return buildYoutubeUploadRequest(
    paths,
    selected?.dataset.playlistMode || 'streamer-default',
    selected?.value || ''
  );
}

function updateAutoRecorderSettingCopy() {
  const toggle = $('autoRecorderEnabled');
  const copy = $('autoRecorderSettingState');
  if (!toggle || !copy) return;
  const persisted = state?.settings?.auto_recorder_enabled === true;
  if (toggle.checked !== persisted) {
    copy.textContent = toggle.checked
      ? 'Will enable when settings are saved.'
      : 'Will pause when settings are saved. Active recordings will continue.';
    return;
  }
  copy.textContent = persisted
    ? 'Enabled · only selected streamers are monitored.'
    : 'Paused · streamer selections are preserved.';
}

function formatAutoRecorderTimestamp(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function autoRecorderStatusPresentation(snapshot) {
  if (!snapshot) {
    return {kind:'loading', title:'Auto Recorder · Checking…', detail:'Loading monitor status.'};
  }
  if (snapshot.unavailable === true) {
    return {kind:'unavailable', title:'Auto Recorder status unavailable', detail:'Will retry automatically.'};
  }
  const watched = Math.max(0, Number(snapshot.watched_count) || 0);
  const selectedText = `${watched} streamer${watched === 1 ? '' : 's'} selected`;
  if (snapshot.state_healthy === false || snapshot.phase === 'degraded') {
    const reason = ['invalid_json', 'invalid_structure', 'unsupported_version'].includes(snapshot.last_error_code)
      ? 'State file invalid'
      : ['state_persistence_failed', 'unreadable_state'].includes(snapshot.last_error_code)
        ? 'State file unavailable'
        : 'Monitor error';
    return {kind:'degraded', title:'Auto Recorder · Degraded', detail:`${reason} · automatic recordings are paused for safety.`};
  }
  if (snapshot.enabled !== true) {
    return {kind:'paused', title:'Auto Recorder · Paused', detail:selectedText};
  }
  if (snapshot.running !== true) {
    return {kind:'unavailable', title:'Auto Recorder · Unavailable', detail:'Production monitor is not running.'};
  }
  if (snapshot.phase === 'starting') {
    return {kind:'starting', title:'Auto Recorder · Starting', detail:selectedText};
  }
  if (watched === 0) {
    return {kind:'running', title:'Auto Recorder · Running', detail:'No streamers selected'};
  }
  const checkedAt = formatAutoRecorderTimestamp(snapshot.last_check_completed_at);
  return {
    kind:'running',
    title:'Auto Recorder · Running',
    detail:`Watching ${watched}${checkedAt ? ` · Last checked ${checkedAt}` : ' · Awaiting first check'}`
  };
}

function renderAutoRecorderStatus(snapshot=autoRecorderStatusSnapshot) {
  const box = $('autoRecorderStatus');
  if (!box) return;
  const view = autoRecorderStatusPresentation(snapshot);
  box.className = `auto-recorder-status is-${view.kind}`;
  box.innerHTML = `<strong>${escapeHtml(view.title)}</strong><span>${escapeHtml(view.detail)}</span>`;
  if (liveStreamers.length) renderLiveStreams();
}

async function refreshAutoRecorderStatus() {
  try {
    const snapshot = await api('/api/auto-recorder/status');
    if (!snapshot || typeof snapshot.enabled !== 'boolean' || typeof snapshot.running !== 'boolean') {
      throw new Error('Invalid Auto Recorder status response.');
    }
    autoRecorderStatusSnapshot = snapshot;
  } catch {
    autoRecorderStatusSnapshot = {unavailable:true};
  }
  renderAutoRecorderStatus();
  return autoRecorderStatusSnapshot;
}

function formatRecordingDuration(value) {
  const seconds = Number(value);
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return [hours, minutes, remainder].map(part => String(part).padStart(2, '0')).join(':');
}

function formatLiveStartedAt(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function recordingJobForStreamer(streamer, jobs=liveRecordingJobs) {
  const login = canonicalStreamerLoginClient(streamer);
  const matching = (jobs || []).filter(job => (
    job?.type === 'recording'
    && canonicalStreamerLoginClient(job.streamer) === login
  ));
  return matching.find(job => ACTIVE_RECORDING_STATES.has(job.state))
    || matching.find(job => job.state === 'completed' || job.state === 'failed')
    || null;
}

function activeRecordingJob(jobs=liveRecordingJobs) {
  return (jobs || []).find(job => (
    job?.type === 'recording' && ACTIVE_RECORDING_STATES.has(job.state)
  )) || null;
}

function recordingStatusText(job) {
  if (!job) return '';
  if (job.state === 'queued') return 'Recording is starting…';
  if (job.state === 'running') return `Recording ${formatRecordingDuration(job.recorded_seconds)}`;
  if (job.state === 'stopping') return 'Recording is stopping…';
  if (job.state === 'completed' && job.completion_reason === 'natural_end' && job.output_complete) return 'Stream ended · recording saved';
  if (job.state === 'completed' && job.completion_reason === 'stopped_by_user' && job.output_complete) return 'Recording saved';
  if (job.state === 'completed' && job.completion_reason === 'natural_end') return 'Stream ended · recording completed';
  if (job.state === 'completed' && job.completion_reason === 'stopped_by_user') return 'Recording stopped';
  if (job.state === 'failed' && job.completion_reason === 'stop_incomplete') return 'Recording could not be saved completely';
  if (job.state === 'failed' && job.completion_reason === 'stop_failed') return 'Recording could not be stopped cleanly';
  if (job.state === 'failed') return 'Recording failed';
  return '';
}

function liveStatusText(status) {
  if (!status || status.state === 'unknown') return 'Not checked yet';
  if (status.state === 'checking') return 'Checking live status…';
  if (status.state === 'live') return 'LIVE';
  if (status.state === 'offline') return 'Offline';
  return 'Status could not be loaded';
}

function liveStatusClass(status, recordingJob) {
  if (recordingJob?.state === 'failed') return 'error';
  if (recordingJob && ACTIVE_RECORDING_STATES.has(recordingJob.state)) return 'recording';
  if (status?.state === 'live') return 'live';
  if (status?.state === 'error') return 'error';
  return status?.state === 'checking' ? 'checking' : 'offline';
}

function liveAutoRecordingNote(streamer, status) {
  if (status?.state !== 'live') return '';
  if (!streamerProfileAutoRecordEnabled(
    state?.settings?.streamer_profiles, streamer
  )) return '';
  if (state?.settings?.auto_recorder_enabled !== true) {
    return 'Auto Recording selected · Auto Recorder paused';
  }
  if (!autoRecorderStatusSnapshot) {
    return 'Auto Recording selected · Checking Auto Recorder';
  }
  if (
    autoRecorderStatusSnapshot.state_healthy === false
    || autoRecorderStatusSnapshot.phase === 'degraded'
  ) {
    return 'Auto Recording selected · Auto Recorder degraded';
  }
  if (autoRecorderStatusSnapshot.running !== true) {
    return 'Auto Recording selected · Auto Recorder unavailable';
  }
  return 'Auto Recording enabled';
}

function liveCardStatusLabel(status, job, action) {
  if (action?.phase === 'starting' || job?.state === 'queued') return 'LIVE · STARTING';
  if (action?.phase === 'stopping' || job?.state === 'stopping') return 'LIVE · STOPPING';
  if (job?.state === 'running') return 'LIVE · RECORDING';
  if (job?.state === 'failed') return 'RECORDING ERROR';
  if (job?.state === 'completed') return 'RECORDING COMPLETE';
  return liveStatusText(status);
}

function liveStreamerIsFeatured(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  const status = liveStreamStatuses.get(login) || {state:'unknown'};
  const job = recordingJobForStreamer(login);
  return !!job || status.state === 'live' || status.state === 'error';
}

function liveStreamerDisplayPriority(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  const status = liveStreamStatuses.get(login) || {state:'unknown'};
  const job = recordingJobForStreamer(login);
  if (job && ACTIVE_RECORDING_STATES.has(job.state)) return 0;
  if (status.state === 'live') return 1;
  if (job?.state === 'failed') return 2;
  if (job?.state === 'completed') return 3;
  if (status.state === 'error') return 4;
  if (status.state === 'checking') return 5;
  return 6;
}

function liveStreamSummaryText(featured, offline) {
  const liveCount = featured.filter(streamer => {
    const login = canonicalStreamerLoginClient(streamer);
    return liveStreamStatuses.get(login)?.state === 'live';
  }).length;
  const recordingCount = featured.filter(streamer => {
    const job = recordingJobForStreamer(streamer);
    return !!job && ACTIVE_RECORDING_STATES.has(job.state);
  }).length;
  return `${liveCount} Live · ${recordingCount} Recording · ${offline.length} Offline`;
}

function renderLiveStreamCard(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  const status = liveStreamStatuses.get(login) || {state:'unknown', streamer:login};
  const job = recordingJobForStreamer(login);
  const activeJob = activeRecordingJob();
  const action = liveRecordingActions.get(login) || null;
  const activeHere = !!job && ACTIVE_RECORDING_STATES.has(job.state);
  const terminalHere = !!job && (job.state === 'completed' || job.state === 'failed');
  const stateClass = liveStatusClass(status, job);
  const statusLabel = liveCardStatusLabel(status, job, action);
  const title = status.state === 'live'
    ? String(status.title || job?.title || '').trim()
    : ((activeHere || terminalHere) ? String(job?.title || '').trim() : '');
  const started = status.state === 'live' ? formatLiveStartedAt(status.started_at) : '';
  const canStart = status.state === 'live' && !activeJob && (!action || action.phase === 'error');
  const startDisabledReason = status.state === 'live' && activeJob && !activeHere
    ? 'Another recording is already active.'
    : '';
  const refreshNote = status.refreshError
    ? 'Status refresh failed; showing the last confirmed status.'
    : (liveStatusRequests.has(login) ? 'Updating live status…' : '');
  const autoRecordingNote = liveAutoRecordingNote(login, status);
  const autoStartedNote = activeHere && job?.origin === 'auto'
    ? 'Started automatically'
    : '';
  let actionHtml = '';
  if (activeHere && job.state === 'running') {
    actionHtml = `<button type="button" class="danger-outline live-recording-stop" data-job-id="${escapeHtml(job.id)}" data-streamer="${escapeHtml(login)}" ${action?.phase === 'stopping' ? 'disabled' : ''}>Stop Recording</button>`;
  } else if (status.state === 'live' && !activeHere) {
    actionHtml = `<button type="button" class="primary live-recording-start" data-streamer="${escapeHtml(login)}" ${canStart ? '' : 'disabled'} title="${escapeHtml(startDisabledReason)}">Start Recording</button>`;
  }
  const secondary = [
    title ? `<span class="live-stream-title">${escapeHtml(title)}</span>` : '',
    started ? `<span class="live-stream-time">Live since ${escapeHtml(started)}</span>` : '',
    (activeHere || terminalHere) ? `<span class="live-recording-status">${escapeHtml(recordingStatusText(job))}</span>` : '',
    autoStartedNote ? `<span class="live-auto-origin-note">${escapeHtml(autoStartedNote)}</span>` : '',
    autoRecordingNote ? `<span class="live-auto-record-note">${escapeHtml(autoRecordingNote)}</span>` : '',
    action?.message ? `<span class="live-recording-message bad">${escapeHtml(action.message)}</span>` : '',
    startDisabledReason ? `<span class="live-recording-note muted">${escapeHtml(startDisabledReason)}</span>` : '',
    refreshNote ? `<span class="live-status-refresh-note${status.refreshError ? ' is-error' : ''}">${escapeHtml(refreshNote)}</span>` : ''
  ].filter(Boolean).join('');
  return `<article class="live-stream-card is-${stateClass}" data-live-streamer="${escapeHtml(login)}">
    <div class="live-stream-indicator" aria-hidden="true"></div>
    <div class="live-stream-copy">
      <span class="live-stream-state">${escapeHtml(statusLabel)}</span>
      <strong class="live-stream-name">${escapeHtml(streamer)}</strong>
      ${secondary}
    </div>
    <div class="live-stream-actions">${actionHtml}</div>
  </article>`;
}

function renderOfflineStreamer(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  const status = liveStreamStatuses.get(login) || {state:'unknown'};
  const refreshLabel = status.refreshError
    ? 'Offline · refresh failed'
    : (liveStatusRequests.has(login) ? 'Offline · updating…' : 'Offline');
  return `<div class="offline-stream-item" data-live-streamer="${escapeHtml(login)}">
    <span class="offline-stream-indicator" aria-hidden="true"></span>
    <strong>${escapeHtml(streamer)}</strong>
    <span>${escapeHtml(refreshLabel)}</span>
  </div>`;
}

function toggleOfflineStreamers() {
  liveOfflineExpanded = !liveOfflineExpanded;
  renderLiveStreams();
}

function renderLiveStreams() {
  const box = $('liveStreamsList');
  if (!box) return;
  const summary = $('liveStreamsSummary');
  if (!liveStreamers.length) {
    if (summary) summary.textContent = '0 Live · 0 Recording · 0 Offline';
    box.innerHTML = '<div class="live-stream-empty muted">No streamers are configured. Add streamers in Settings.</div>';
    return;
  }
  const indexed = liveStreamers.map((streamer, index) => ({streamer, index}));
  const featured = indexed
    .filter(item => liveStreamerIsFeatured(item.streamer))
    .sort((left, right) => (
      liveStreamerDisplayPriority(left.streamer) - liveStreamerDisplayPriority(right.streamer)
      || left.index - right.index
    ))
    .map(item => item.streamer);
  const offline = indexed
    .filter(item => {
      const login = canonicalStreamerLoginClient(item.streamer);
      return liveStreamStatuses.get(login)?.state === 'offline'
        && !liveStreamerIsFeatured(item.streamer);
    })
    .map(item => item.streamer);
  if (summary) summary.textContent = liveStreamSummaryText(featured, offline);
  const initialCheckPending = liveStatusLastUpdatedAt === null && (
    liveStatusRefreshPromise !== null
    || liveStatusRequests.size > 0
    || indexed.some(item => {
      const login = canonicalStreamerLoginClient(item.streamer);
      const state = liveStreamStatuses.get(login)?.state || 'unknown';
      return state === 'unknown' || state === 'checking';
    })
  );
  const liveContent = featured.length
    ? `<div class="live-stream-grid${featured.length === 1 ? ' is-single' : ''}">${featured.map(streamer => renderLiveStreamCard(streamer)).join('')}</div>`
    : `<div class="live-stream-empty muted">${initialCheckPending ? 'Checking live status…' : 'No configured streamer is currently live.'}</div>`;
  const offlineContent = offline.length
    ? `<section class="offline-streams">
        <button type="button" class="offline-streams-toggle" aria-expanded="${liveOfflineExpanded ? 'true' : 'false'}" aria-controls="offlineStreamersList" aria-label="${liveOfflineExpanded ? 'Hide' : 'Show'} ${offline.length} offline streamers">
          <span>Offline Streamers · ${offline.length}</span><span>${liveOfflineExpanded ? 'Hide' : 'Show'}</span>
        </button>
        <div id="offlineStreamersList" class="offline-stream-grid"${liveOfflineExpanded ? '' : ' hidden'}>${offline.map(streamer => renderOfflineStreamer(streamer)).join('')}</div>
      </section>`
    : '';
  box.innerHTML = liveContent + offlineContent;
  box.querySelectorAll('.live-recording-start').forEach(button => button.addEventListener('click', () => {
    startLiveRecording(button.dataset.streamer).catch(() => {});
  }));
  box.querySelectorAll('.live-recording-stop').forEach(button => button.addEventListener('click', () => {
    stopLiveRecording(button.dataset.jobId, button.dataset.streamer).catch(() => {});
  }));
  box.querySelector('.offline-streams-toggle')?.addEventListener('click', toggleOfflineStreamers);
}

function syncLiveStreamers(streamers) {
  const seen = new Set();
  liveStreamers = (streamers || []).filter(streamer => {
    const login = canonicalStreamerLoginClient(streamer);
    if (!login || seen.has(login)) return false;
    seen.add(login);
    if (!liveStreamStatuses.has(login)) liveStreamStatuses.set(login, {state:'unknown', streamer:login});
    return true;
  });
  const configured = new Set(liveStreamers.map(canonicalStreamerLoginClient));
  [...liveStreamStatuses.keys()].forEach(login => {
    if (!configured.has(login)) liveStreamStatuses.delete(login);
  });
  renderLiveStreams();
}

function updateLiveRecordingJobs(jobs) {
  liveRecordingJobs = (jobs || []).filter(job => job?.type === 'recording');
  liveStreamers.forEach(streamer => {
    const login = canonicalStreamerLoginClient(streamer);
    const action = liveRecordingActions.get(login);
    const job = recordingJobForStreamer(login);
    if (action?.phase === 'starting' && job && ACTIVE_RECORDING_STATES.has(job.state)) liveRecordingActions.delete(login);
    if (action?.phase === 'stopping' && job && !ACTIVE_RECORDING_STATES.has(job.state)) liveRecordingActions.delete(login);
    if (action?.phase === 'error' && job && ACTIVE_RECORDING_STATES.has(job.state)) liveRecordingActions.delete(login);
  });
  renderLiveStreams();
}

async function requestLiveStatus(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  if (!login) return null;
  if (liveStatusRequests.has(login)) return liveStatusRequests.get(login);
  const previousStatus = liveStreamStatuses.get(login) || {state:'unknown', streamer:login};
  const hasConfirmedStatus = previousStatus.state === 'live' || previousStatus.state === 'offline';
  liveStreamStatuses.set(login, hasConfirmedStatus
    ? {...previousStatus, refreshError:false}
    : {state:'checking', streamer:login});
  const request = api(`/api/live/status?streamer=${encodeURIComponent(login)}`)
    .then(payload => {
      const nextState = payload?.state === 'live' || payload?.state === 'offline'
        ? payload.state
        : 'error';
      liveStreamStatuses.set(login, {...payload, streamer:login, state:nextState});
      return liveStreamStatuses.get(login);
    })
    .catch(() => {
      const failed = hasConfirmedStatus
        ? {...previousStatus, streamer:login, refreshError:true}
        : {state:'error', streamer:login};
      liveStreamStatuses.set(login, failed);
      return failed;
    })
    .finally(() => {
      liveStatusRequests.delete(login);
      renderLiveStreams();
    });
  liveStatusRequests.set(login, request);
  renderLiveStreams();
  return request;
}

async function refreshLiveStatuses() {
  if (liveStatusRefreshPromise) return liveStatusRefreshPromise;
  const button = $('refreshLiveStatuses');
  const message = $('liveStreamsRefreshStatus');
  if (button) button.disabled = true;
  const queue = [...liveStreamers];
  let completed = 0;
  if (message) message.textContent = queue.length ? `Updating ${completed} / ${queue.length}` : 'No configured streamers to check.';
  liveStatusRefreshPromise = (async () => {
    let index = 0;
    const worker = async () => {
      while (index < queue.length) {
        const streamer = queue[index++];
        await requestLiveStatus(streamer);
        completed += 1;
        if (message) message.textContent = `Updating ${completed} / ${queue.length}`;
      }
    };
    const workerCount = Math.min(LIVE_STATUS_CONCURRENCY, queue.length);
    await Promise.all(Array.from({length:workerCount}, worker));
  })();
  try {
    await liveStatusRefreshPromise;
  } finally {
    liveStatusRefreshPromise = null;
    if (button) button.disabled = false;
    if (queue.length) {
      liveStatusLastUpdatedAt = new Date();
      if (message) message.textContent = `Updated ${formatLiveStartedAt(liveStatusLastUpdatedAt)}`;
    } else if (message) {
      message.textContent = 'No configured streamers to check.';
    }
    renderLiveStreams();
  }
}

function initializeLiveStatuses() {
  if (liveStatusInitialRefreshStarted) return;
  liveStatusInitialRefreshStarted = true;
  refreshLiveStatuses().catch(() => {});
}

function friendlyRecordingActionError(error, action) {
  const code = String(error?.message || '').split(/\n/)[0];
  if (code === 'recording_conflict') return 'Another recording is already active.';
  if (code === 'streamer_not_live') return 'The streamer is no longer live.';
  if (code === 'live_status_unavailable') return 'Live status could not be confirmed.';
  if (code === 'recording_not_stoppable') return 'This recording can no longer be stopped.';
  return action === 'stop' ? 'Recording could not be stopped.' : 'Recording could not be started.';
}

async function startLiveRecording(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  if (!login) return;
  liveRecordingActions.set(login, {phase:'starting'});
  renderLiveStreams();
  try {
    await api('/api/live/record', {method:'POST', body:JSON.stringify({streamer:login})});
  } catch (error) {
    liveRecordingActions.set(login, {phase:'error', message:friendlyRecordingActionError(error, 'start')});
    renderLiveStreams();
    showToast(friendlyRecordingActionError(error, 'start'), 'bad');
    throw error;
  }
  pollJobs().catch(() => {});
}

async function stopLiveRecording(jobId, streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  if (!login || !jobId) return;
  liveRecordingActions.set(login, {phase:'stopping'});
  renderLiveStreams();
  try {
    await api(`/api/live/record/${encodeURIComponent(jobId)}/stop`, {method:'POST', body:JSON.stringify({})});
    pollJobs().catch(() => {});
  } catch (error) {
    liveRecordingActions.set(login, {phase:'error', message:friendlyRecordingActionError(error, 'stop')});
    renderLiveStreams();
    showToast(friendlyRecordingActionError(error, 'stop'), 'bad');
    throw error;
  }
}

function streamerEditorNames() {
  return String($('streamersText')?.value || '').split(/\r?\n/).map(name => name.trim()).filter(Boolean);
}

function setStreamerEditorNames(names) {
  $('streamersText').value = (names || []).join('\n');
  renderStreamerEditor();
}

function renderStreamerEditor() {
  const list = $('streamerEditorList');
  if (!list || !$('streamersText')) return;
  const names = streamerEditorNames();
  if (!names.length) {
    list.innerHTML = '<div class="streamer-editor-empty muted">No streamers configured yet.</div>';
    return;
  }
  list.innerHTML = names.map((name, index) => {
    const playlistId = streamerProfilePlaylistId(
      streamerProfileDraft, name
    );
    const autoRecord = streamerProfileAutoRecordEnabled(
      streamerProfileDraft, name
    );
    return `<div class="streamer-editor-row" data-streamer-index="${index}"><span class="streamer-order">${index + 1}</span><strong>${escapeHtml(name)}</strong><label class="streamer-playlist-field"><span>Default Playlist</span><select class="streamer-playlist-select" data-streamer-login="${escapeHtml(name)}" aria-label="Default YouTube playlist for ${escapeHtml(name)}">${playlistOptionsHtml(playlistId, 'Global Default')}</select></label><label class="streamer-auto-record-field"><span>Auto Recording</span><span class="switch-control"><input type="checkbox" role="switch" class="streamer-auto-record-toggle" data-streamer-login="${escapeHtml(name)}" aria-label="Enable automatic recording for ${escapeHtml(name)}" ${autoRecord ? 'checked' : ''}><span class="switch-track" aria-hidden="true"></span><span class="switch-value">${autoRecord ? 'On' : 'Off'}</span></span></label><div class="streamer-row-actions"><button type="button" data-streamer-action="up" aria-label="Move ${escapeHtml(name)} up" ${index === 0 ? 'disabled' : ''}>Up</button><button type="button" data-streamer-action="down" aria-label="Move ${escapeHtml(name)} down" ${index === names.length - 1 ? 'disabled' : ''}>Down</button><button type="button" class="danger-outline" data-streamer-action="remove" aria-label="Remove ${escapeHtml(name)}">Remove</button></div></div>`;
  }).join('');
  list.querySelectorAll('.streamer-playlist-select').forEach(select => {
    select.addEventListener('change', () => {
      streamerProfileDraft = withStreamerPlaylistSelection(
        streamerProfileDraft,
        select.dataset.streamerLogin,
        select.value
      );
    });
  });
  list.querySelectorAll('.streamer-auto-record-toggle').forEach(toggle => {
    toggle.addEventListener('change', () => {
      streamerProfileDraft = withStreamerAutoRecordSelection(
        streamerProfileDraft,
        toggle.dataset.streamerLogin,
        toggle.checked
      );
      const value = toggle.closest('.switch-control')?.querySelector('.switch-value');
      if (value) value.textContent = toggle.checked ? 'On' : 'Off';
    });
  });
  list.querySelectorAll('[data-streamer-action]').forEach(button => button.addEventListener('click', () => {
    const row = button.closest('[data-streamer-index]');
    const index = Number(row.dataset.streamerIndex);
    const current = streamerEditorNames();
    const action = button.dataset.streamerAction;
    if (action === 'remove') current.splice(index, 1);
    if (action === 'up' && index > 0) [current[index - 1], current[index]] = [current[index], current[index - 1]];
    if (action === 'down' && index < current.length - 1) [current[index + 1], current[index]] = [current[index], current[index + 1]];
    setStreamerEditorNames(current);
  }));
}

function addStreamerFromInput() {
  const input = $('streamerAddInput');
  const raw = String(input?.value || '').trim();
  if (!raw) return;
  const names = streamerEditorNames();
  if (names.some(name => name.toLowerCase() === raw.toLowerCase())) {
    showToast('That streamer is already in the list.', 'warn');
    return;
  }
  names.push(raw);
  setStreamerEditorNames(names);
  input.value = '';
  input.focus();
}

function showSettingsTab(name) {
  const allowed = ['general', 'streamers', 'youtube', 'advanced'];
  const target = allowed.includes(name) ? name : 'general';
  document.querySelectorAll('.settings-tab').forEach(tab => {
    const active = tab.dataset.settingsTab === target;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.settings-panel').forEach(panel => panel.classList.toggle('active', panel.dataset.settingsPanel === target));
  try { localStorage.setItem('vodSettingsTab', target); } catch {}
  if (target === 'youtube') {
    refreshYoutubeStatus().catch(() => {});
  }
  if (target === 'youtube' || target === 'streamers') {
    loadYoutubePlaylists().catch(() => {});
  }
}

function rememberSearchResults(results) {
  const compact = (results || []).slice(0, 300).map(r => ({
    url: r.url,
    streamer: r.streamer || '',
    title: r.title || '',
    date: r.date || ''
  }));
  try { localStorage.setItem('vodSearchResultMetadata', JSON.stringify(compact)); } catch {}
}

function rememberedSearchResults() {
  try {
    const value = JSON.parse(localStorage.getItem('vodSearchResultMetadata') || '[]');
    return Array.isArray(value) ? value : [];
  } catch { return []; }
}

async function downloadSelectedWithConfirm() {
  const selected = selectedResultObjects();
  if (!selected.length) {
    alert('No VODs selected.');
    return;
  }
  const groups = {};
  selected.forEach(r => { groups[r.streamer] = (groups[r.streamer] || 0) + 1; });
  const groupLines = Object.entries(groups).map(([name, count]) => `${name}: ${count} VOD(s)`).join('\n');
  const ok = confirm(`Download ${selected.length} VOD(s)?

${groupLines}

Review the streamer list before starting.`);
  if (!ok) return;
  const data = await startDownload(selected.map(r => r.url), 'Date Range Selection');
  const batchModeLabel = ($('batchPostprocessMode') && $('batchPostprocessMode').value === 'after_all') ? 'Download all, then post-process' : 'Post-process after each VOD';
  alert(`Download queue started: ${data.url_count || selected.length} VOD(s).\n\nMode: ${batchModeLabel}`);
  showPage('queue');
}

function renderResults(results, errors, debug) {
  lastResults = results || [];
  rememberSearchResults(lastResults);
  const errHtml = errors && errors.length ? errors.map(e => `<div class="errorbox"><b>${escapeHtml(e.streamer)}</b>: ${escapeHtml(e.error)}</div>`).join('') : '';
  const dbgHtml = debug && debug.length ? debug.map(d => `${escapeHtml(d.streamer)}: ${d.kept}/${d.deduped || d.found_raw} shown · raw source results: ${d.found_raw} · date metadata enriched: ${d.date_metadata_enriched || 0} · date enrichment failed: ${d.date_enrichment_failed || 0} · unknown date: ${d.unknown_dates} · outside date range: ${d.skipped_by_date} · live/upcoming filtered: ${d.skipped_live || 0} · non-VOD filtered: ${d.skipped_nonvod || 0}`).join('<br>') : 'No diagnostic details returned.';
  $('searchErrors').innerHTML = errHtml;
  if ($('searchDiagnostics')) $('searchDiagnostics').innerHTML = dbgHtml;
  if ($('searchResultSummary')) $('searchResultSummary').textContent = `${lastResults.length} VOD${lastResults.length === 1 ? '' : 's'} found`;
  const body = $('resultsBody');
  if (!lastResults.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No matching VODs found. Try expanding the date range.</td></tr>';
    refreshSelectionState();
    return;
  }

  const groups = new Map();
  lastResults.forEach(r => {
    const key = r.streamer || 'unknown';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  });

  const rows = [];
  for (const [streamer, items] of groups.entries()) {
    const openCount = items.filter(r => !r.already_downloaded).length;
    rows.push(`<tr class="streamer-group-row"><td colspan="6"><div class="streamer-group-head"><div><strong>${escapeHtml(streamer)}</strong><span>${items.length} VOD(s), ${openCount} ready to download</span></div><div><button type="button" class="group-select" data-streamer="${escapeHtml(streamer)}">Select All</button><button type="button" class="group-clear" data-streamer="${escapeHtml(streamer)}">Clear</button></div></div></td></tr>`);
    items.forEach(r => {
      rows.push(`
        <tr>
          <td><input class="rowcheck" type="checkbox" data-url="${escapeHtml(r.url)}" data-streamer="${escapeHtml(r.streamer)}" data-already-downloaded="${r.already_downloaded ? 'true' : 'false'}"></td>
          <td data-label="Date">${escapeHtml(r.date)}</td>
          <td data-label="Streamer">${escapeHtml(r.streamer)}</td>
          <td data-label="Title">${escapeHtml(r.title)}</td>
          <td data-label="Status" class="${r.already_downloaded ? 'good' : ''}">${r.already_downloaded ? 'Already in Archive' : 'Ready to Download'}${r.outside_range ? '<br><span class="warn">Outside Date Range</span>' : ''}</td>
          <td data-label="Link"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">Open Twitch</a></td>
        </tr>`);
    });
  }
  body.innerHTML = rows.join('');
  const all = $('checkAll');
  if (all) all.checked = false;
  document.querySelectorAll('.rowcheck').forEach(cb => cb.addEventListener('change', refreshSelectionState));
  document.querySelectorAll('.group-select').forEach(btn => btn.addEventListener('click', () => setStreamerSelection(btn.dataset.streamer, true)));
  document.querySelectorAll('.group-clear').forEach(btn => btn.addEventListener('click', () => setStreamerSelection(btn.dataset.streamer, false)));
  refreshSelectionState();
}

async function searchVods() {
  $('resultsBody').innerHTML = `<tr><td colspan="6" class="muted">Searching...</td></tr>`;
  if ($('searchResultSummary')) $('searchResultSummary').textContent = 'Searching...';
  $('downloadSelected').disabled = true;
  const streamers = selectedSearchStreamersForSearch();
  if (!streamers.length || !streamers[0]) { alert('No streamers are configured. Save at least one streamer first.'); return; }
  const data = await api('/api/search', { method:'POST', body: JSON.stringify({ streamers, from: $('fromDate').value, to: $('toDate').value, limit: $('limit').value, include_unknown_dates: $('includeUnknownDates').checked, strict_date_filter: $('strictDateFilter').checked, exclude_live_streams: $('excludeLiveStreams').checked, only_real_vod_urls: $('onlyRealVodUrls').checked }) });
  renderResults(data.results, data.errors, data.debug);
}


function fixTemplateFieldsBeforeSave() {
  const output = $('outputTemplate');
  const manual = $('manualUploadFilenameTemplate');

  if (output) {
    const v = (output.value || '').trim();
    if (!v || (v.includes('{') && !v.includes('%('))) {
      output.value = YTDLP_DEFAULT_OUTPUT_TEMPLATE;
    }
  }

  if (manual) {
    const v = (manual.value || '').trim();
    if (!v || v.includes('%(')) {
      manual.value = MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE;
    }
  }
}

function gatherSettingsFromForm() {
  fixTemplateFieldsBeforeSave();
  return {
    download_path:$('downloadPath').value,
    streamer_file:$('streamerFile').value,
    archive_file:$('archiveFile').value,
    cookie_browser:$('cookieBrowser').value,
    quality:$('quality').value,
    fragments:$('fragments').value,
    twitch_rate_limit:$('twitchRateLimit').value,
    batch_postprocess_mode:$('batchPostprocessMode').value,
    merge_format:$('mergeFormat').value,
    include_unknown_dates:$('includeUnknownDates').checked,
    exclude_live_streams:$('excludeLiveStreams').checked,
    only_real_vod_urls:$('onlyRealVodUrls').checked,
    strict_date_filter:$('strictDateFilter').checked,
    output_template:$('outputTemplate').value,
    playlist_end:$('limit').value,
    auto_recorder_enabled:$('autoRecorderEnabled').checked,
    youtube_enabled:$('youtubeEnabled').checked,
    youtube_auto_upload:$('youtubeAutoUpload').checked,
    move_uploaded_vods:$('moveUploadedVods').checked,
    uploaded_vods_folder:$('uploadedVodsFolder').value,
    youtube_privacy_status:$('youtubePrivacyStatus').value,
    youtube_playlist_id:$('youtubePlaylistId').value,
    youtube_client_secret_file:$('youtubeClientSecretFile').value,
    youtube_token_file:$('youtubeTokenFile').value,
    youtube_description:$('youtubeDescription').value,
    youtube_tags:$('youtubeTags').value,
    youtube_category_id:$('youtubeCategoryId').value,
    youtube_title_template:$('youtubeTitleTemplate').value,
    youtube_description_template:$('youtubeDescriptionTemplate').value,
    youtube_chunk_size_mb:$('youtubeChunkSizeMb').value,
    youtube_upload_mode:$('youtubeUploadMode').value,
    manual_upload_filename_template:$('manualUploadFilenameTemplate').value,
    manual_upload_prepare_enabled:$('manualUploadPrepareEnabled').checked,
    manual_upload_rename_video:$('manualUploadRenameVideo').checked,
    manual_upload_write_description:$('manualUploadWriteDescription').checked,
    manual_upload_write_metadata_json:$('manualUploadWriteMetadataJson').checked
  };
}

function markSettingsSaved(message) {
  const el = document.getElementById('settingsSaveStatus');
  if (el) el.textContent = message || 'saved';
}

async function saveCurrentSettingsSilently() {
  state.settings = await api('/api/settings', { method:'POST', body: JSON.stringify(gatherSettingsFromForm()) });
  if (state.settings && state.settings._settings_file && document.getElementById('settingsFilePath')) {
    document.getElementById('settingsFilePath').textContent = state.settings._settings_file;
  }
  markSettingsSaved('saved: ' + (state.settings._saved_at || new Date().toLocaleTimeString()));
}

async function startDownload(urls, label) {
  await saveCurrentSettingsSilently();
  const cleanUrls = [...new Set((urls || []).filter(Boolean))];
  if (!cleanUrls.length) throw new Error('No VOD URLs selected.');
  const data = await api('/api/download', { method:'POST', body: JSON.stringify({ urls: cleanUrls, label }) });
  await pollJobs();
  return data;
}

function collectOpenStates() {
  document.querySelectorAll('.job-details').forEach(el => {
    const id = el.dataset.jobId;
    if (id) jobOpenState[id] = !!el.open;
  });
  document.querySelectorAll('[data-queue-detail-id]').forEach(el => {
    const id = el.dataset.queueDetailId;
    if (id) queueDetailOpenState[id] = !!el.open;
  });
}

function parseProgress(logs) {
  const info = {
    downloadProgress: null,
    uploadProgress: null,
    latestLine: 'No activity yet.',
    urlCount: null,
    downloadSpeed: '',
    uploadFile: '',
    activeStage: 'wartet',
    startedUploads: 0,
    finishedUploads: 0,
    eta: '',
    currentItem: '',
    batchCurrent: null,
    batchTotal: null,
  };
  for (const raw of (logs || []).slice().reverse()) {
    const line = String(raw || '').trim();
    if (line) { info.latestLine = line; break; }
  }
  for (const line of (logs || [])) {
    let m = line.match(/--- VOD\s+(\d+)\/(\d+)\s+---/i);
    if (m) {
      info.batchCurrent = Number(m[1]);
      info.batchTotal = Number(m[2]);
      info.currentItem = `VOD ${m[1]}/${m[2]}`;
    }
    m = line.match(/URLs:\s*(\d+)/i);
    if (m) info.urlCount = Number(m[1]);
    m = line.match(/\[download\]\s+(\d+(?:\.\d+)?)%.*?at\s+([^\s]+)\/s(?:.*?ETA\s+([^\s]+))?/i);
    if (m) {
      info.downloadProgress = Number(m[1]);
      info.downloadSpeed = m[2] + '/s';
      if (m[3]) info.eta = m[3];
      info.activeStage = 'download';
    }
    m = line.match(/YouTube Upload (?:starting|startet):\s*(.+?)\s*\((private|unlisted|public)\)/i);
    if (m) {
      info.uploadFile = m[1];
      info.startedUploads += 1;
      info.activeStage = 'upload';
    }
    m = line.match(/YouTube Upload\s+(.+?):\s*(\d+)%/i);
    if (m) {
      info.uploadFile = m[1];
      info.uploadProgress = Number(m[2]);
      info.activeStage = 'upload';
    }
    if (/YouTube Upload (?:completed|fertig):/i.test(line)) {
      info.finishedUploads += 1;
      info.activeStage = 'upload';
      if (info.uploadProgress === null || info.uploadProgress < 100) info.uploadProgress = 100;
    }
  }
  return info;
}

function parseEtaSeconds(value) {
  const parts = String(value || '').trim().split(':');
  if (parts.length < 2 || parts.length > 3 || parts.some(part => !/^\d+$/.test(part))) return null;
  const numbers = parts.map(Number);
  const seconds = parts.length === 3
    ? numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    : numbers[0] * 60 + numbers[1];
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

function formatRemainingDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 60) return `${Math.ceil(seconds)} sec remaining`;
  const totalMinutes = Math.ceil(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} min remaining`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours} hr${hours === 1 ? '' : 's'}${minutes ? ` ${minutes} min` : ''} remaining`;
}

function formatProcessedDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 60) return `${Math.floor(seconds)} sec processed`;
  const totalMinutes = Math.floor(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} min processed`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours} hr${hours === 1 ? '' : 's'}${minutes ? ` ${minutes} min` : ''} processed`;
}

function formatTransferSpeed(value) {
  if (value === null || value === undefined || value === '') return '';
  const bytesPerSecond = Number(value);
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond < 0) return '';
  if (bytesPerSecond < 1024) return `${Math.round(bytesPerSecond)} B/s`;
  if (bytesPerSecond < 1024 ** 2) return `${(bytesPerSecond / 1024).toFixed(1)} KB/s`;
  if (bytesPerSecond < 1024 ** 3) return `${(bytesPerSecond / (1024 ** 2)).toFixed(1)} MB/s`;
  return `${(bytesPerSecond / (1024 ** 3)).toFixed(2)} GB/s`;
}

function currentEtaSeconds(value, updatedAt, nowMs=Date.now()) {
  const etaSeconds = Number(value);
  if (!Number.isFinite(etaSeconds) || etaSeconds <= 0) return null;
  const timestamp = Number(updatedAt);
  const nowSeconds = Number(nowMs) / 1000;
  const age = Number.isFinite(timestamp) && timestamp > 0 && timestamp <= nowSeconds
    ? Math.max(0, nowSeconds - timestamp)
    : 0;
  const remaining = Math.ceil(etaSeconds - age);
  return remaining > 0 ? remaining : null;
}

function overallRunningEstimate(items, nowMs=Date.now()) {
  const etas = (items || [])
    .filter(item => item && (item.state === 'running' || item.state === 'cancelling'))
    .map(item => Number(item.etaSeconds))
    .filter(value => Number.isFinite(value) && value > 0);
  if (!etas.length) return null;
  const etaSeconds = Math.max(...etas);
  const completion = new Date(nowMs + etaSeconds * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', hour12: false
  });
  return {
    etaSeconds,
    completionLabel: `Estimated completion ${completion}`,
    remainingLabel: formatRemainingDuration(etaSeconds),
  };
}

function renderOverallRunningEstimate(items) {
  const box = $('queueRunningEstimate');
  if (!box) return;
  const estimate = overallRunningEstimate(items);
  box.classList.toggle('hidden', !estimate);
  box.innerHTML = estimate
    ? `<span>${escapeHtml(estimate.completionLabel)}</span><strong>${escapeHtml(estimate.remainingLabel)}</strong>`
    : '';
}

function niceStatus(status) {
  if (status === 'läuft') return 'Running';
  if (status === 'fertig') return 'Completed';
  if (status === 'fehler') return 'Error';
  return 'Queued';
}

function statusClass(status) {
  if (status === 'fertig') return 'good';
  if (status === 'fehler') return 'bad';
  if (status === 'läuft') return 'accent';
  return 'muted';
}

function stageLabel(job, info) {
  if (job.status === 'fehler') return 'Error';
  if (job.status === 'fertig' && info.uploadProgress !== null) return 'Upload Completed';
  if (job.status === 'fertig') return 'Download Completed';
  if (info.activeStage === 'upload') return 'YouTube Upload';
  if (info.activeStage === 'download') return 'Download';
  return 'Preparing';
}

function renderProgressBar(label, progress, extra = '') {
  if (progress === null || Number.isNaN(progress)) return '';
  const pct = Math.max(0, Math.min(100, progress));
  return `
    <div class="progress-block">
      <div class="progress-head"><span>${escapeHtml(label)}</span><span>${pct.toFixed(pct % 1 ? 1 : 0)}%${extra ? ' · ' + escapeHtml(extra) : ''}</span></div>
      <div class="progress-bar"><span style="width:${pct}%"></span></div>
    </div>`;
}

function updateJobDetailButtonLabel() {
  return;
}


function updateQueueSummary(jobs) {
  const list = jobs || [];
  const active = list.filter(j => j.status === 'läuft').length;
  const done = list.filter(j => j.status === 'fertig').length;
  const failed = list.filter(j => j.status === 'fehler').length;
  if ($('queueTotal')) $('queueTotal').textContent = String(list.length);
  if ($('queueActive')) $('queueActive').textContent = String(active);
  if ($('queueDone')) $('queueDone').textContent = String(done);
  if ($('queueFailed')) $('queueFailed').textContent = String(failed);

  const activeJob = list.find(j => j.status === 'läuft');
  if (!activeJob) {
    if ($('queueCurrent')) $('queueCurrent').textContent = list.length ? 'No active job' : 'Nothing active';
    if ($('queueEta')) $('queueEta').textContent = '';
    return;
  }
  const info = parseProgress(activeJob.log || []);
  const activeItem = queueItemsFromJobs([activeJob]).find(item => item.state === 'running' || item.state === 'cancelling');
  const current = [activeJob.label, info.currentItem].filter(Boolean).join(' · ');
  if ($('queueCurrent')) $('queueCurrent').textContent = current || activeJob.label;
  if ($('queueEta')) {
    $('queueEta').textContent = activeItem
      ? [activeItem.progress !== null ? `${activeItem.progress}%` : '', activeItem.extra].filter(Boolean).join(' · ')
      : [
        info.downloadProgress !== null ? `${info.downloadProgress}%` : '',
        info.downloadSpeed,
        formatRemainingDuration(parseEtaSeconds(info.eta))
      ].filter(Boolean).join(' · ');
  }
}

function queueMetadataByUrl(url) {
  const all = [...lastResults, ...rememberedSearchResults()];
  return all.find(item => item && item.url === url) || {};
}

function queuePathName(path) {
  return String(path || '').split(/[\\/]/).filter(Boolean).pop() || 'Local VOD';
}

function queueVodId(url) {
  const match = String(url || '').match(/(?:videos\/)(\d+)/);
  return match ? match[1] : '';
}

function downloadLogSegment(logs, index) {
  const startPattern = new RegExp(`--- VOD\\s+${index}\\/\\d+\\s+---`, 'i');
  const start = logs.findIndex(line => startPattern.test(String(line)));
  if (start < 0) return [];
  const next = logs.findIndex((line, pos) => pos > start && /--- VOD\s+\d+\/\d+\s+---/i.test(String(line)));
  return logs.slice(start, next < 0 ? logs.length : next);
}

function normalizeQueueFileIdentity(value) {
  return String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/g, '').toLocaleLowerCase();
}

function uploadLogSegment(logs, path) {
  const markers = [];
  (logs || []).forEach((line, position) => {
    const match = String(line || '').match(/^Uploading local VOD file:\s*(.+)$/i);
    if (match) markers.push({position, identity: normalizeQueueFileIdentity(match[1])});
  });
  const identity = normalizeQueueFileIdentity(path);
  let markerIndex = markers.findIndex(marker => marker.identity === identity);
  if (markerIndex < 0) {
    const name = normalizeQueueFileIdentity(queuePathName(path));
    const matchingNames = markers
      .map((marker, index) => ({marker, index}))
      .filter(value => normalizeQueueFileIdentity(queuePathName(value.marker.identity)) === name);
    if (matchingNames.length === 1) markerIndex = matchingNames[0].index;
  }
  if (markerIndex < 0) return [];
  const start = markers[markerIndex].position;
  const next = markers[markerIndex + 1];
  return logs.slice(start, next ? next.position : logs.length);
}

function queueErrorFromLines(lines) {
  return [...(lines || [])].reverse().find(line => /(?:failed|error|ended with error|did not start)/i.test(String(line))) || '';
}

function queueErrorSummary(raw, operation) {
  let message = String(raw || '').trim();
  message = message.replace(/^YouTube Upload failed for [^:]+:\s*/i, '');
  message = message.replace(/^Local YouTube upload did not start:\s*/i, '');
  message = message.replace(/^Error:\s*/i, '');
  message = message.replace(/^[A-Za-z_][\w.]*Error:\s*/, '');
  const exitCode = message.match(/ended with error code\s+(-?\d+)/i);
  if (exitCode) return `Download failed (exit code ${exitCode[1]}).`;
  return message ? `${operation || 'Operation failed'}: ${message}` : `${operation || 'Operation'} failed. See technical details below.`;
}

function queueItemKey(item) {
  return `${item.job.type || 'download'}:${item.job.id}:${item.itemId || item.index}`;
}

function explicitQueueItemState(job, index) {
  const state = Array.isArray(job.item_states) ? job.item_states[index] : '';
  return ({queued:'waiting', running:'running', cancelling:'cancelling', completed:'completed', failed:'error', cancelled:'cancelled', interrupted:'interrupted'})[state] || '';
}

function queueItemListValue(job, key, index, fallback='') {
  const values = Array.isArray(job?.[key]) ? job[key] : [];
  return index >= 0 && index < values.length ? values[index] : fallback;
}

function queueItemsFromJobs(jobs, nowMs=Date.now()) {
  const items = [];
  (jobs || []).slice().reverse().forEach(job => {
    if (job?.type === 'recording') {
      const zeroIndex = 0;
      const stateName = explicitQueueItemState(job, zeroIndex) || (job.state === 'failed' ? 'error' : job.state);
      if (!['interrupted', 'error'].includes(stateName)) return;
      const itemId = queueItemListValue(job, 'item_ids', zeroIndex, `${job.id}-item-1`);
      items.push({
        job, itemId,
        capabilities: queueItemListValue(job, 'item_capabilities', zeroIndex, {}),
        state: stateName,
        operation: stateName === 'interrupted' ? 'Recording interrupted' : 'Recording failed',
        resolved: !!queueItemListValue(job, 'item_resolved', zeroIndex, false),
        streamer: job.streamer || '', date: '', title: job.title || job.label,
        vodId: '', filename: '', sizeBytes: null, sizeGb: null,
        progress: null, etaSeconds: null, updatedAt: null, extra: '',
        historicalProgress: null,
        historicalProcessedSeconds: Number(job.recorded_seconds) || null,
        detailLogs: Array.isArray(job.log) ? job.log : [],
        error: stateName === 'error' ? queueErrorFromLines(job.log || []) : '',
        completionReason: queueItemListValue(job, 'item_completion_reasons', zeroIndex, job.completion_reason || ''),
        recoveryReason: queueItemListValue(job, 'item_recovery_reasons', zeroIndex, job.recovery_reason || ''),
        failureKind: queueItemListValue(job, 'item_failure_kinds', zeroIndex, ''),
        index: zeroIndex,
      });
      return;
    }
    const logs = job.log || [];
    const progress = parseProgress(logs);
    const sources = job.urls || [];
    if (job.type === 'youtube_upload') {
      sources.forEach((path, zeroIndex) => {
        const name = queuePathName(path);
        const segment = uploadLogSegment(logs, path);
        const local = localVideoCache.get(path) || [...localVideoCache.values()].find(v => v.name === name) || {};
        const metadata = (Array.isArray(job.item_metadata) && job.item_metadata[zeroIndex]) || {};
        const itemId = (Array.isArray(job.item_ids) && job.item_ids[zeroIndex]) || `${job.id}-item-${zeroIndex + 1}`;
        const capabilities = (Array.isArray(job.item_capabilities) && job.item_capabilities[zeroIndex]) || {};
        const trackedStatus = Array.isArray(job.item_statuses) ? job.item_statuses[zeroIndex] : '';
        const trackedProgress = Array.isArray(job.item_progress) ? job.item_progress[zeroIndex] : null;
        const trackedBytesUploaded = Array.isArray(job.item_bytes_uploaded) ? job.item_bytes_uploaded[zeroIndex] : null;
        const trackedTotalBytes = Array.isArray(job.item_total_bytes) ? job.item_total_bytes[zeroIndex] : null;
        const trackedBytesPerSecond = Array.isArray(job.item_bytes_per_second) ? job.item_bytes_per_second[zeroIndex] : null;
        const trackedEtaSeconds = Array.isArray(job.item_eta_seconds) ? job.item_eta_seconds[zeroIndex] : null;
        const trackedUpdatedAt = Array.isArray(job.item_updated_at) ? job.item_updated_at[zeroIndex] : null;
        const trackedError = Array.isArray(job.item_errors) ? job.item_errors[zeroIndex] : '';
        const resolved = !!(Array.isArray(job.item_resolved) && job.item_resolved[zeroIndex]);
        const failure = [...segment].reverse().find(line => /(?:failed|error|without a video ID)/i.test(String(line))) || '';
        const completion = segment.some(line => /YouTube Upload completed:/i.test(String(line)));
        const started = segment.some(line => /(?:Uploading local VOD file:|YouTube Upload starting:)/i.test(String(line)));
        const explicitState = explicitQueueItemState(job, zeroIndex);
        let stateName = explicitState || 'waiting';
        if (!explicitState) {
          if (trackedStatus === 'fehler') stateName = 'error';
          else if (trackedStatus === 'fertig') stateName = 'completed';
          else if (trackedStatus === 'läuft') stateName = 'running';
          else if (trackedStatus === 'wartet') stateName = 'waiting';
          else if (failure) stateName = 'error';
          else if (completion || job.status === 'fertig') stateName = 'completed';
          else if (job.status === 'läuft' && progress.uploadFile && progress.uploadFile.includes(name)) stateName = 'running';
          else if (job.status === 'läuft' && started && !progress.uploadFile) stateName = 'running';
          else if (job.status === 'fehler') stateName = 'error';
        }
        const operation = stateName === 'running' ? 'Uploading to YouTube' : stateName === 'cancelling' ? 'Cancelling YouTube upload' : stateName === 'completed' ? 'YouTube upload completed' : stateName === 'error' ? 'YouTube upload failed' : stateName === 'cancelled' ? 'YouTube upload cancelled' : stateName === 'interrupted' ? 'YouTube upload interrupted' : 'Waiting to upload';
        const activeTransfer = stateName === 'running' || stateName === 'cancelling';
        const etaSeconds = activeTransfer ? currentEtaSeconds(trackedEtaSeconds, trackedUpdatedAt, nowMs) : null;
        const speedLabel = activeTransfer ? formatTransferSpeed(trackedBytesPerSecond) : '';
        items.push({
          job, itemId, capabilities, state: stateName, operation, resolved,
          streamer: metadata.streamer || local.streamer || '', date: metadata.date || local.date_de || '', title: metadata.title || local.title || local.youtube_title || name,
          vodId: metadata.vod_id || local.vod_id || '', filename: queuePathName(metadata.name || local.name || name),
          sizeBytes: metadata.size_bytes ?? local.size_bytes ?? null, sizeGb: metadata.size_gb ?? local.size_gb ?? null,
          progress: activeTransfer ? trackedProgress : null,
          bytesUploaded: activeTransfer ? trackedBytesUploaded : null,
          totalBytes: activeTransfer ? trackedTotalBytes : null,
          bytesPerSecond: activeTransfer ? trackedBytesPerSecond : null,
          etaSeconds,
          updatedAt: activeTransfer ? trackedUpdatedAt : null,
          extra: [speedLabel, formatRemainingDuration(etaSeconds)].filter(Boolean).join(' · '),
          historicalProgress: activeTransfer ? null : trackedProgress,
          historicalProcessedSeconds: null,
          detailLogs: segment,
          error: trackedError || failure || (stateName === 'error' ? queueErrorFromLines(segment) : ''),
          completionReason: queueItemListValue(job, 'item_completion_reasons', zeroIndex, job.completion_reason || ''),
          recoveryReason: queueItemListValue(job, 'item_recovery_reasons', zeroIndex, job.recovery_reason || ''),
          failureKind: queueItemListValue(job, 'item_failure_kinds', zeroIndex, ''),
          index: zeroIndex
        });
      });
      return;
    }

    sources.forEach((url, zeroIndex) => {
      const index = zeroIndex + 1;
      const meta = queueMetadataByUrl(url);
      const segment = downloadLogSegment(logs, index);
      const failed = segment.some(line => /ended with error code/i.test(String(line)));
      const completed = segment.some(line => new RegExp(`VOD\\s+${index}\\/\\d+ download completed`, 'i').test(String(line)));
      const itemId = (Array.isArray(job.item_ids) && job.item_ids[zeroIndex]) || `${job.id}-item-${zeroIndex + 1}`;
      const capabilities = (Array.isArray(job.item_capabilities) && job.item_capabilities[zeroIndex]) || {};
      const trackedStatus = Array.isArray(job.item_statuses) ? job.item_statuses[zeroIndex] : '';
      const trackedProgress = Array.isArray(job.item_progress) ? job.item_progress[zeroIndex] : null;
      const trackedProcessedSeconds = Array.isArray(job.item_processed_seconds) ? job.item_processed_seconds[zeroIndex] : null;
      const trackedSpeedLabel = Array.isArray(job.item_speed_label) ? job.item_speed_label[zeroIndex] : '';
      const trackedEtaSeconds = Array.isArray(job.item_eta_seconds) ? job.item_eta_seconds[zeroIndex] : null;
      const trackedUpdatedAt = Array.isArray(job.item_updated_at) ? job.item_updated_at[zeroIndex] : null;
      const resolved = !!(Array.isArray(job.item_resolved) && job.item_resolved[zeroIndex]);
      const explicitState = explicitQueueItemState(job, zeroIndex);
      let stateName = explicitState || 'waiting';
      if (!explicitState) {
        if (trackedStatus === 'fehler') stateName = 'error';
        else if (trackedStatus === 'fertig') stateName = 'completed';
        else if (trackedStatus === 'läuft') stateName = 'running';
        else if (trackedStatus === 'wartet' && job.status === 'fehler') stateName = 'error';
        else if (trackedStatus === 'wartet' && job.status === 'fertig') stateName = 'completed';
        else if (trackedStatus === 'wartet') stateName = 'waiting';
        else if (failed) stateName = 'error';
        else if (completed || job.status === 'fertig') stateName = 'completed';
        else if (job.status === 'läuft' && progress.batchCurrent === index) stateName = 'running';
        else if (job.status === 'läuft' && progress.batchCurrent && progress.batchCurrent > index) stateName = 'completed';
        else if (job.status === 'fehler') stateName = 'error';
      }
      const vodId = queueVodId(url);
      const operation = stateName === 'running' ? 'Downloading' : stateName === 'cancelling' ? 'Cancelling download' : stateName === 'completed' ? 'Download completed' : stateName === 'error' ? 'Download failed' : stateName === 'cancelled' ? 'Download cancelled' : stateName === 'interrupted' ? 'Download interrupted' : 'Waiting to download';
      const hasStructuredDownloadState = Array.isArray(job.item_progress) && Array.isArray(job.item_eta_seconds);
      const structuredProgress = Number(trackedProgress);
      const hasStructuredProgress = trackedProgress !== null && trackedProgress !== '' && Number.isFinite(structuredProgress);
      const activeTransfer = stateName === 'running' || stateName === 'cancelling';
      const displayedProgress = activeTransfer
        ? (hasStructuredProgress ? structuredProgress : (hasStructuredDownloadState ? null : progress.downloadProgress))
        : null;
      const hasStructuredEta = trackedEtaSeconds !== null && trackedEtaSeconds !== '' && Number.isFinite(Number(trackedEtaSeconds));
      const structuredEta = activeTransfer && hasStructuredEta
        ? currentEtaSeconds(trackedEtaSeconds, trackedUpdatedAt, nowMs)
        : null;
      const etaSeconds = activeTransfer
        ? (hasStructuredEta ? structuredEta : (hasStructuredDownloadState ? null : parseEtaSeconds(progress.eta)))
        : null;
      const speedLabel = activeTransfer
        ? (/^\d+(?:\.\d+)?x$/i.test(String(trackedSpeedLabel || '').trim())
          ? ''
          : (trackedSpeedLabel || (hasStructuredDownloadState ? '' : progress.downloadSpeed)))
        : '';
      const processedLabel = activeTransfer && displayedProgress === null
        ? formatProcessedDuration(trackedProcessedSeconds)
        : '';
      items.push({
        job, itemId, capabilities, state: stateName, operation, resolved,
        streamer: meta.streamer || '', date: meta.date || '', title: meta.title || (vodId ? `Twitch VOD ${vodId}` : job.label),
        vodId, filename: '', sizeBytes: null, sizeGb: null,
        progress: displayedProgress,
        processedSeconds: activeTransfer ? trackedProcessedSeconds : null,
        etaSeconds,
        updatedAt: activeTransfer ? trackedUpdatedAt : null,
        extra: activeTransfer ? [processedLabel, speedLabel, formatRemainingDuration(etaSeconds)].filter(Boolean).join(' · ') : '',
        historicalProgress: activeTransfer ? null : (hasStructuredProgress ? structuredProgress : null),
        historicalProcessedSeconds: activeTransfer ? null : trackedProcessedSeconds,
        detailLogs: segment,
        error: stateName === 'error' ? queueErrorFromLines(segment.length ? segment : logs) : '',
        completionReason: queueItemListValue(job, 'item_completion_reasons', zeroIndex, job.completion_reason || ''),
        recoveryReason: queueItemListValue(job, 'item_recovery_reasons', zeroIndex, job.recovery_reason || ''),
        failureKind: queueItemListValue(job, 'item_failure_kinds', zeroIndex, ''),
        index: zeroIndex
      });
    });
  });
  return items;
}

function conciseQueueFilename(value, maxLength=72) {
  const name = queuePathName(value);
  if (name.length <= maxLength) return name;
  const keep = Math.max(12, Math.floor((maxLength - 3) / 2));
  return `${name.slice(0, keep)}...${name.slice(-keep)}`;
}

function queueFileSizeLabel(item) {
  const bytes = Number(item.sizeBytes);
  if (Number.isFinite(bytes) && bytes > 0) {
    const gib = bytes / (1024 ** 3);
    if (gib >= 1) return `${gib.toFixed(gib >= 10 ? 1 : 2)} GB`;
    return `${(bytes / (1024 ** 2)).toFixed(1)} MB`;
  }
  const gb = Number(item.sizeGb);
  return Number.isFinite(gb) && gb > 0 ? `${gb} GB` : '';
}

function distinguishQueueItems(items) {
  const groups = new Map();
  (items || []).forEach(item => {
    const key = [item.streamer || 'Unknown streamer', item.date || 'Unknown date', item.title || item.job.label]
      .map(value => String(value).trim().toLocaleLowerCase()).join('\u0000');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return (items || []).map(item => {
    const key = [item.streamer || 'Unknown streamer', item.date || 'Unknown date', item.title || item.job.label]
      .map(value => String(value).trim().toLocaleLowerCase()).join('\u0000');
    const colliding = groups.get(key) || [];
    if (colliding.length < 2) return {...item, distinguishingLabel: ''};
    const parts = [];
    if (item.vodId) parts.push(`VOD ID ${item.vodId}`);
    const size = queueFileSizeLabel(item);
    if (size) parts.push(size);
    let label = parts.join(' \u00b7 ');
    const labels = colliding.map(candidate => {
      const candidateParts = [];
      if (candidate.vodId) candidateParts.push(`VOD ID ${candidate.vodId}`);
      const candidateSize = queueFileSizeLabel(candidate);
      if (candidateSize) candidateParts.push(candidateSize);
      return candidateParts.join(' \u00b7 ');
    });
    if (!label || labels.filter(value => value === label).length > 1) {
      const filename = conciseQueueFilename(item.filename || '');
      if (filename && !parts.includes(filename)) parts.push(filename);
      label = parts.join(' \u00b7 ');
    }
    const withFilenames = colliding.map(candidate => {
      const candidateParts = [];
      if (candidate.vodId) candidateParts.push(`VOD ID ${candidate.vodId}`);
      const candidateSize = queueFileSizeLabel(candidate);
      if (candidateSize) candidateParts.push(candidateSize);
      const filename = conciseQueueFilename(candidate.filename || '');
      if (filename && !candidateParts.includes(filename)) candidateParts.push(filename);
      return candidateParts.join(' \u00b7 ');
    });
    if (!label || withFilenames.filter(value => value === label).length > 1) {
      parts.push(`Queue item ${item.job.id}-${item.index + 1}`);
      label = parts.join(' \u00b7 ');
    }
    return {...item, distinguishingLabel: label};
  });
}

function queueRecoveryPresentation(item) {
  const type = item.job?.type === 'youtube_upload' ? 'upload' : item.job?.type === 'recording' ? 'recording' : 'download';
  const reason = String(item.recoveryReason || item.completionReason || '');
  const uncertainUpload = type === 'upload' && (reason === 'upload_status_unknown' || item.failureKind === 'uncertain');
  if (uncertainUpload) return {
    status:'Upload status uncertain',
    support:'The dashboard restarted while YouTube may have been processing this upload. Check YouTube Studio before uploading it again.',
    reviewRequired:true,
  };
  if (item.state !== 'interrupted') return {
    status:({running:item.operation, cancelling:'Cancelling...', waiting:'Queued', completed:'Completed', error:'Failed', cancelled:'Cancelled'})[item.state] || 'Queued',
    support:'', reviewRequired:false,
  };
  if (type === 'download') return {
    status:'Download interrupted',
    support: reason === 'restart_before_start'
      ? 'The dashboard restarted before this download started.'
      : 'The dashboard restarted while this download was running.',
    reviewRequired:false,
  };
  if (type === 'upload') return {
    status:'Upload interrupted',
    support: reason === 'restart_before_start'
      ? 'The dashboard restarted before the YouTube upload started.'
      : 'The dashboard restarted before this upload finished.',
    reviewRequired:false,
  };
  return {
    status:'Recording interrupted',
    support: reason === 'restart_before_start'
      ? 'The dashboard restarted before recording started.'
      : 'The dashboard restarted while this recording was active.',
    reviewRequired:false,
  };
}

function queueJobTypeLabel(item) {
  return item.job?.type === 'youtube_upload' ? 'YouTube upload' : item.job?.type === 'recording' ? 'Recording' : 'Download';
}

function queueTechnicalDetailsHtml(item, itemId, detailId, detailOpen, error) {
  const itemLogs = Array.isArray(item.detailLogs) ? item.detailLogs : [];
  const logText = itemLogs.length
    ? itemLogs.slice(-120).join('\n')
    : item.state === 'interrupted'
      ? 'Detailed process log was not retained across restart.'
      : 'No item-specific technical log is available.';
  const values = [
    ['Job ID', item.job.id],
    ['Item ID', itemId],
    ['Type', queueJobTypeLabel(item)],
    ['Created', item.job.created_at || item.job.created || ''],
    ['Started', item.job.started_at || ''],
    ['Finished / interrupted', item.job.finished_at || ''],
    ['Completion reason', item.completionReason || ''],
    ['Recovery reason', item.recoveryReason || ''],
  ];
  const retryJobId = String(item.capabilities?.retry_job_id || '');
  if (retryJobId) values.push(['Retry job', retryJobId]);
  if (item.job?.retry_of?.job_id) values.push(['Retry of', `Job ${item.job.retry_of.job_id}`]);
  if (item.job?.type === 'recording') {
    if (item.job.origin) values.push(['Origin', item.job.origin]);
    if (item.job.attempt) values.push(['Attempt', item.job.attempt]);
  }
  const historicalProgress = Number(item.historicalProgress);
  if (item.historicalProgress !== null && item.historicalProgress !== '' && Number.isFinite(historicalProgress)) {
    values.push(['Last recorded progress', `${Math.max(0, Math.min(100, historicalProgress)).toFixed(1).replace(/\.0$/, '')}%`]);
  }
  const historicalSeconds = Number(item.historicalProcessedSeconds);
  if (item.historicalProcessedSeconds !== null && Number.isFinite(historicalSeconds) && historicalSeconds > 0) {
    values.push([item.job?.type === 'recording' ? 'Last recorded duration' : 'Last processed duration', formatProcessedDuration(historicalSeconds)]);
  }
  const grid = values.filter(([, value]) => value !== null && value !== undefined && String(value).trim()).map(([label, value]) => `<div><span class="muted">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
  return `<details class="technical-details queue-row-details" data-queue-detail-id="${escapeHtml(detailId)}"${detailOpen}><summary>${item.state === 'error' ? 'View error' : 'Technical details'}</summary><div class="job-detail-grid">${grid}</div>${error}<pre>${escapeHtml(logText)}</pre></details>`;
}

function renderQueueVodItem(item, compact=false) {
  const capabilities = item.capabilities || {};
  const itemId = item.itemId || `${item.job.id}-item-${item.index + 1}`;
  const identity = [item.streamer, item.date].filter(Boolean).join(' · ');
  const presentation = queueRecoveryPresentation(item);
  const status = presentation.status;
  const activeTransfer = item.state === 'running' || item.state === 'cancelling';
  const progress = activeTransfer ? renderProgressBar(item.operation, item.progress, item.extra) : '';
  const progressDetails = activeTransfer && !progress && item.extra
    ? `<div class="queue-progress-text muted">${escapeHtml(item.extra)}</div>`
    : '';
  const detailId = queueItemKey(item);
  const detailOpen = queueDetailOpenState[detailId] ? ' open' : '';
  const error = item.state === 'error' && item.error ? `<div class="queue-item-error">${escapeHtml(queueErrorSummary(item.error, item.operation))}</div>` : '';
  const actionButtons = [];
  const accessibleTitle = String(item.title || item.job.label || queueJobTypeLabel(item));
  if (capabilities.can_cancel) actionButtons.push(`<button type="button" class="danger-outline queue-item-action" data-queue-action="cancel" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Cancel ${escapeHtml(accessibleTitle)}">Cancel</button>`);
  if (capabilities.can_stop_after_current) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="stop" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Stop Queue after ${escapeHtml(accessibleTitle)}">Stop after current</button>`);
  if (capabilities.can_remove) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="remove" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Remove ${escapeHtml(accessibleTitle)} from Queue">Remove from Queue</button>`);
  if (capabilities.can_retry) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="retry" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Retry ${escapeHtml(status)}: ${escapeHtml(accessibleTitle)}">Retry</button>`);
  if (item.state === 'error' && !item.resolved && capabilities.can_resolve !== false) actionButtons.push(`<button type="button" class="quiet-button queue-resolve-error" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}">Mark as resolved</button>`);
  const retryJobId = String(capabilities.retry_job_id || '');
  const retryRelationship = retryJobId ? `<div class="queue-item-note queue-retry-relationship">Retry started as Job ${escapeHtml(retryJobId)}</div>` : '';
  const reviewRequired = presentation.reviewRequired ? '<div class="queue-review-required" role="note">Review required</div>' : '';
  const support = presentation.support ? `<div class="queue-item-support">${escapeHtml(presentation.support)}</div>` : '';
  const actions = actionButtons.length ? `<div class="queue-item-actions">${actionButtons.join('')}</div>` : '';
  const attention = item.state === 'interrupted' ? 'has-attention' : item.state === 'error' ? 'has-error' : '';
  const pillClass = item.state === 'error' ? 'bad' : item.state === 'interrupted' ? 'attention' : item.state === 'completed' ? 'good' : activeTransfer ? 'accent' : 'muted';
  const details = queueTechnicalDetailsHtml(item, itemId, detailId, detailOpen, error);
  if (compact) {
    return `<article class="queue-vod-item compact ${attention} ${presentation.reviewRequired ? 'is-uncertain' : ''}">
      <div class="queue-row-identity"><strong>${escapeHtml(item.streamer || (item.job?.type === 'recording' ? 'Twitch recording' : 'Unknown streamer'))}</strong><span>${escapeHtml(item.date || queueJobTypeLabel(item))}</span></div>
      <div class="queue-row-title">${escapeHtml(item.title || item.job.label)}</div>
      ${item.distinguishingLabel ? `<div class="queue-row-disambiguator muted">${escapeHtml(item.distinguishingLabel)}</div>` : ''}
      <span class="pill ${pillClass}">${escapeHtml(status)}</span>
      ${support}${reviewRequired}${retryRelationship}${actions}${details}
    </article>`;
  }
  return `<article class="queue-vod-item ${attention} ${presentation.reviewRequired ? 'is-uncertain' : ''}">
    <div class="queue-vod-main"><div class="queue-vod-copy">${identity ? `<div class="queue-vod-identity">${escapeHtml(identity)}</div>` : ''}<strong>${escapeHtml(item.title || item.job.label)}</strong></div><span class="pill ${pillClass}">${escapeHtml(status)}</span></div>
    ${support}${error}${reviewRequired}${retryRelationship}${progress}${progressDetails}${actions}${details}
  </article>`;
}

function renderQueueGroup(id, items, emptyMessage, compact=false) {
  const box = $(id);
  if (!box) return;
  box.classList.toggle('muted', !items.length);
  box.innerHTML = items.length ? items.map(item => renderQueueVodItem(item, compact)).join('') : escapeHtml(emptyMessage);
  box.querySelectorAll('.queue-resolve-error').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api('/api/jobs/resolve-error', {method:'POST', body:JSON.stringify({job_id:button.dataset.jobId, item_id:button.dataset.itemId})});
      await pollJobs();
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'bad');
    }
  }));
  const actionRoutes = {
    cancel: ['/api/jobs/cancel-item', 'Cancelling...'],
    stop: ['/api/jobs/stop-after-current', 'Queue will stop after the current item.'],
    remove: ['/api/jobs/remove-item', 'Removed from Queue. The local file was not deleted.'],
    retry: ['/api/jobs/retry-item', 'Starting a fresh retry...'],
  };
  box.querySelectorAll('.queue-item-action').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.queueAction;
    const route = actionRoutes[action];
    if (!route) return;
    button.disabled = true;
    showToast(route[1]);
    try {
      const result = await api(route[0], {method:'POST', body:JSON.stringify({job_id:button.dataset.jobId, item_id:button.dataset.itemId})});
      if (action === 'retry' && result.retry_job_id) showToast(`Retry started as Job ${result.retry_job_id}.`);
      await pollJobs();
    } catch (error) {
      button.disabled = false;
      showToast(friendlyQueueActionError(error), 'bad');
    }
  }));
}

function friendlyQueueActionError(error) {
  const messages = {
    review_required:'Check YouTube Studio before uploading this video again.',
    source_missing:'Source video is no longer available.',
    source_changed:'Source video changed since this upload was queued.',
    unsafe_source_path:'Source video can no longer be used safely.',
    recording_retry_unsupported:'Historical recordings cannot be retried.',
    already_retried:'A retry has already been started for this item.',
    not_retryable:'This Queue item cannot be retried.',
    persistence_unavailable:'Job history persistence is unavailable. No durable change was made.',
    persistence_validation_failed:'Job history could not be saved safely. No durable change was made.',
  };
  return messages[String(error?.reason || '')] || String(error?.message || 'The Queue action could not be completed.');
}

function renderQueueLaneControls(queueControls={}) {
  const box = $('queueLaneControls');
  if (!box) return;
  const lanes = [
    ['download', 'Downloads'],
    ['youtube_upload', 'Uploads'],
  ];
  box.innerHTML = lanes.map(([lane, label]) => {
    const control = queueControls[lane] || {};
    const paused = !!control.queue_paused;
    const note = paused
      ? (control.stop_after_current && control.has_active_item ? 'Stops after current' : 'Queue paused')
      : 'Queue running';
    return `<div class="queue-lane-control"><span><strong>${label}</strong><small>${note}</small></span><button type="button" class="quiet-button queue-lane-action" data-lane="${lane}" data-action="${paused ? 'resume' : 'pause'}">${paused ? 'Resume Queue' : 'Pause Queue'}</button></div>`;
  }).join('');
  box.querySelectorAll('.queue-lane-action').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    const action = button.dataset.action;
    showToast(action === 'pause' ? 'Active work continues; no new item will start.' : 'Queue resumed.');
    try {
      await api(`/api/queue/${action}`, {method:'POST', body:JSON.stringify({lane:button.dataset.lane})});
      await pollJobs();
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'bad');
    }
  }));
}

function queueHistoryTimestamp(item) {
  const values = [item.job?.finished_at, item.job?.updated_at, item.job?.created_at, item.job?.created];
  for (const value of values) {
    const parsed = Date.parse(String(value || '').replace(' ', 'T'));
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function queueHistoryNewest(items) {
  return (items || []).slice().sort((left, right) => {
    const byTime = queueHistoryTimestamp(right) - queueHistoryTimestamp(left);
    if (byTime) return byTime;
    const leftId = Number(left.job?.id);
    const rightId = Number(right.job?.id);
    if (Number.isFinite(leftId) && Number.isFinite(rightId) && rightId !== leftId) return rightId - leftId;
    const byJob = String(right.job?.id || '').localeCompare(String(left.job?.id || ''), undefined, {numeric:true});
    return byJob || Number(right.index || 0) - Number(left.index || 0);
  });
}

function renderQueuePersistenceStatus(status={}) {
  const box = $('queuePersistenceWarning');
  if (!box) return;
  if (!status.enabled || (!status.current_degraded && !status.history_degraded)) {
    box.innerHTML = '';
    box.hidden = true;
    return;
  }
  if (status.current_degraded && status.history_degraded) {
    box.innerHTML = '<strong>Job history persistence degraded</strong><span>Current work can continue, but recent history may not survive a restart. Some saved job history could not be restored.</span>';
  } else if (status.current_degraded) {
    box.innerHTML = '<strong>Job history persistence degraded</strong><span>Current work can continue, but recent job history may not survive a restart.</span>';
  } else {
    box.innerHTML = '<strong>Some saved job history could not be restored.</strong><span>Current downloads and uploads can continue normally.</span>';
  }
  box.hidden = false;
}

function renderVodQueue(jobs, queueControls={}, persistenceStatus={}) {
  const items = queueItemsFromJobs(jobs);
  const running = items.filter(item => item.state === 'running' || item.state === 'cancelling');
  const waiting = items.filter(item => item.state === 'waiting');
  const errors = distinguishQueueItems(queueHistoryNewest(items.filter(item => (item.state === 'error' || item.state === 'interrupted') && !item.resolved)));
  const completed = distinguishQueueItems(queueHistoryNewest(items.filter(item => item.state === 'completed')));
  const cancelled = distinguishQueueItems(queueHistoryNewest(items.filter(item => item.state === 'cancelled')));
  renderQueueGroup('queueRunning', running, 'No downloads or uploads are currently running.');
  renderQueueGroup('queueWaiting', waiting, 'Nothing is waiting.', true);
  renderQueueGroup('queueErrors', errors, 'No jobs need attention.', true);
  renderQueueGroup('queueCompleted', completed, 'No completed jobs.', true);
  renderQueueGroup('queueCancelled', cancelled, 'No cancelled jobs.', true);
  renderQueueLaneControls(queueControls);
  renderQueuePersistenceStatus(persistenceStatus);
  renderOverallRunningEstimate(running);
  if ($('queueActive')) $('queueActive').textContent = String(running.length);
  if ($('queueWaitingCount')) $('queueWaitingCount').textContent = String(waiting.length);
  if ($('queueFailed')) $('queueFailed').textContent = String(errors.length);
  if ($('queueDone')) $('queueDone').textContent = String(completed.length);
  if ($('queueCancelledCount')) $('queueCancelledCount').textContent = String(cancelled.length);
  if ($('clearCompletedJobs')) {
    $('clearCompletedJobs').disabled = completed.length === 0;
    $('clearCompletedJobs').classList.toggle('hidden', completed.length === 0);
  }
  if ($('queueCancelledSection')) $('queueCancelledSection').classList.toggle('hidden', cancelled.length === 0);
  if ($('queueErrorsSection')) $('queueErrorsSection').classList.toggle('hidden', errors.length === 0);
  return {items, running, waiting, errors, completed, cancelled};
}


async function pollJobs() {
  collectOpenStates();
  const data = await api('/api/jobs');
  collectOpenStates();
  const box = $('jobs');
  updateQueueSummary(data.jobs || []);
  renderVodQueue(data.jobs || [], data.queue_controls || {}, data.persistence_status || {});
  updateLiveRecordingJobs(data.jobs || []);
  if (!data.jobs.length) {
    box.textContent = 'No downloads in this session yet.';
    updateQueueSummary([]);
    updateJobDetailButtonLabel();
    return;
  }
  box.classList.remove('muted');
  box.innerHTML = data.jobs.map(j => {
    const logs = j.log || [];
    const info = parseProgress(logs);
    if (!(j.id in jobOpenState)) jobOpenState[j.id] = autoExpandJobDetails;
    const openAttr = jobOpenState[j.id] ? ' open' : '';
    const logText = logs.slice(-120).join('\n');
    const pills = [
      `<span class="pill ${statusClass(j.status)}">${escapeHtml(niceStatus(j.status))}</span>`,
      `<span class="pill">${escapeHtml(stageLabel(j, info))}</span>`,
      info.urlCount ? `<span class="pill">${info.urlCount} URL${info.urlCount === 1 ? '' : 's'}</span>` : '',
      info.startedUploads ? `<span class="pill">Uploads: ${info.finishedUploads}/${info.startedUploads}</span>` : ''
    ].filter(Boolean).join('');
    const shortLog = escapeHtml(info.latestLine);
    return `
      <div class="job ${j.status === 'fehler' ? 'job-error' : ''}">
        <div class="job-top">
          <div>
            <div class="job-title">${escapeHtml(j.label)}</div>
            <div class="job-meta">${escapeHtml(j.created)}</div>
          </div>
          <div class="job-pills">${pills}</div>
        </div>
        <div class="job-summary">${shortLog}</div>
        ${renderProgressBar('Download', info.downloadProgress, [info.downloadSpeed, info.eta ? 'ETA ' + info.eta : ''].filter(Boolean).join(' · '))}
        ${renderProgressBar('YouTube Upload', info.uploadProgress, info.uploadFile)}
        <details class="job-details" data-job-id="${escapeHtml(j.id)}"${openAttr}>
          <summary>${jobOpenState[j.id] ? 'Hide Details' : 'Show Details'}</summary>
          <div class="job-detail-grid">
            <div><span class="muted">Job ID</span><strong>${escapeHtml(j.id)}</strong></div>
            <div><span class="muted">Status</span><strong>${escapeHtml(niceStatus(j.status))}</strong></div>
            <div><span class="muted">Stage</span><strong>${escapeHtml(stageLabel(j, info))}</strong></div>
            <div><span class="muted">Latest Activity</span><strong>${escapeHtml(info.latestLine.slice(0, 120))}</strong></div>
          </div>
          <pre>${escapeHtml(logText)}</pre>
        </details>
      </div>`;
  }).join('');

  document.querySelectorAll('.job-details').forEach(el => {
    el.addEventListener('toggle', () => {
      jobOpenState[el.dataset.jobId] = el.open;
      updateJobDetailButtonLabel();
      el.querySelector('summary').textContent = el.open ? 'Hide Details' : 'Show Details';
    });
  });
  updateJobDetailButtonLabel();
}


async function refreshDashboard() {
  try {
    const [data, jobsData] = await Promise.all([api('/api/dashboard'), api('/api/jobs')]);
    const yt = data.youtube || {};
    const disk = data.disk || {};
    const queue = queueItemsFromJobs(jobsData.jobs || []);
    updateLiveRecordingJobs(jobsData.jobs || []);
    const running = queue.filter(item => item.state === 'running' || item.state === 'cancelling');
    const waiting = queue.filter(item => item.state === 'waiting');
    const errors = queue.filter(item => item.state === 'error' && !item.resolved);
    const hasActivity = running.length > 0 || waiting.length > 0;
    renderQueueGroup('dashboardRunning', running.slice(0, 4), 'No downloads or uploads are currently running.', true);
    renderQueueGroup('dashboardUpcoming', waiting.slice(0, 5), 'Nothing is waiting.', true);
    $('dashboardRunningSection').classList.toggle('hidden', !hasActivity);
    $('dashboardUpcomingSection').classList.toggle('hidden', !hasActivity);

    const alerts = [];
    if (errors.length) alerts.push(`<article class="action-alert bad-alert"><div><strong>${errors.length} VOD${errors.length === 1 ? '' : 's'} need attention</strong><span>${escapeHtml(errors[0].title || errors[0].job.label)}${errors.length > 1 ? ` and ${errors.length - 1} more` : ''}</span></div><button type="button" class="goto-page" data-page="queue">View Error${errors.length === 1 ? '' : 's'}</button></article>`);
    if (state && state.settings && state.settings.youtube_enabled && !yt.connected) alerts.push('<article class="action-alert warn-alert"><div><strong>YouTube is not connected</strong><span>Connect YouTube before starting an upload.</span></div><button type="button" class="goto-page" data-page="settings" data-settings-target="youtube">Open YouTube Settings</button></article>');
    if (disk.ok && disk.free_gb < 50) alerts.push(`<article class="action-alert warn-alert"><div><strong>Storage is running low</strong><span>${escapeHtml(disk.free_gb)} GB is available for VOD downloads.</span></div><button type="button" class="goto-page" data-page="settings" data-settings-target="advanced">Review Settings</button></article>`);
    $('dashboardAlerts').innerHTML = alerts.join('');
    document.querySelectorAll('#page-dashboard .goto-page').forEach(btn => btn.onclick = () => {
      showPage(btn.dataset.page);
      if (btn.dataset.settingsTarget) showSettingsTab(btn.dataset.settingsTarget);
    });
  } catch (e) {
    if ($('dashboardAlerts')) $('dashboardAlerts').innerHTML = `<article class="action-alert bad-alert"><div><strong>Dashboard status could not be loaded</strong><span>${escapeHtml(e.message)}</span></div></article>`;
  }
}

const UPLOADED_HISTORY_PAGE_SIZE = 20;
let uploadedHistoryVisibleCount = UPLOADED_HISTORY_PAGE_SIZE;
let localVideoCache = new Map();

function selectedLocalVideoPaths() {
  return [...document.querySelectorAll('.localvideocheck:checked')].map(cb => cb.dataset.path);
}

function updateLocalUploadButton() {
  const selected = selectedLocalVideoPaths().length;
  const uploadBtn = $('uploadSelectedLocalVideos');
  if (uploadBtn) uploadBtn.disabled = selected === 0;
}

function workspaceStatusClass(video) {
  if (video.local_file_exists === false) return 'warn';
  if (video.already_uploaded) return 'good';
  if (video.in_uploaded_folder) return 'accent';
  if (video.manually_uploaded || video.dashboard_uploaded) return 'good';
  if (video.prepared) return 'warn';
  return 'muted';
}

function workspaceStatusLabel(video) {
  return video.status || 'Ready';
}

function localVideoByPath(path) {
  return localVideoCache.get(path) || null;
}

function renderLocalVideoCard(v) {
  const uploaded = !!v.already_uploaded;
  const hasLocalFile = v.local_file_exists !== false;
  const uploadable = !v.already_uploaded && hasLocalFile;
  const statusClassName = workspaceStatusClass(v);
  const secondaryStatus = uploaded
    ? 'Uploaded to YouTube'
    : (v.prepared ? 'Metadata ready' : 'Metadata needed');
  return `<article class="video-workspace-card ${uploaded ? 'is-uploaded' : ''} ${hasLocalFile ? '' : 'is-local-removed'}" data-video-path="${escapeHtml(v.path)}">
    ${uploadable ? `<label class="video-select"><input class="localvideocheck" type="checkbox" data-path="${escapeHtml(v.path)}" checked><span>Select</span></label>` : '<span class="video-select muted">History</span>'}
    <div class="video-person"><strong>${escapeHtml(v.streamer || 'Unknown streamer')}</strong><span>${escapeHtml(v.date_de || 'Unknown date')}</span></div>
    <strong class="video-display-title">${escapeHtml(v.title || v.youtube_title || v.name)}</strong>
    <span class="video-size">${hasLocalFile ? `${escapeHtml(v.size_gb)} GB` : 'Size unavailable'}</span>
    <span class="metadata-status ${uploaded || v.prepared ? 'good' : 'muted'}">${secondaryStatus}</span>
    <span class="pill ${statusClassName}">${escapeHtml(workspaceStatusLabel(v))}</span>
    <div class="video-primary-actions">${uploadable ? `<button type="button" class="primary video-action" data-action="upload" data-path="${escapeHtml(v.path)}">Upload</button>` : ''}</div>
    ${hasLocalFile ? `<details class="technical-details secondary-actions"><summary>Actions</summary><div class="video-copy-actions"><button type="button" class="video-action" data-action="copy-title" data-path="${escapeHtml(v.path)}">Copy Title</button><button type="button" class="video-action" data-action="copy-description" data-path="${escapeHtml(v.path)}">Copy Description</button>${uploadable && !v.prepared ? `<button type="button" class="video-action" data-action="prepare" data-path="${escapeHtml(v.path)}">Prepare metadata</button>` : ''}${uploadable ? `<button type="button" class="video-action" data-action="mark" data-path="${escapeHtml(v.path)}">Mark as Uploaded</button>` : ''}</div><div class="danger-zone"><strong>Delete the local VOD file and its sidecars</strong><button type="button" class="danger-outline video-action" data-action="delete" data-path="${escapeHtml(v.path)}">Delete Permanently</button></div></details>` : '<span class="muted">Upload history retained; local actions are unavailable.</span>'}
  </article>`;
}

function visibleLocalVideoRows(videos, includeUploaded, historyLimit=UPLOADED_HISTORY_PAGE_SIZE) {
  const pending = (videos || []).filter(video => !video.already_uploaded);
  if (!includeUploaded) return pending;
  const uploaded = (videos || []).filter(video => video.already_uploaded);
  return [...pending, ...uploaded.slice(0, Math.max(0, historyLimit))];
}

async function loadLocalVideos() {
  const includeUploaded = !!($('includeUploadedLocalVideos') && $('includeUploadedLocalVideos').checked);
  const data = await api('/api/local-videos?include_uploaded=' + (includeUploaded ? '1' : '0'));
  const box = $('localVideoCards');
  const info = $('localVideosInfo');
  if (!box) return;

  const videos = data.videos || [];
  localVideoCache = new Map(videos.map(v => [v.path, v]));
  const counts = data.counts || {};
  const visibleVideos = visibleLocalVideoRows(
    videos, includeUploaded, uploadedHistoryVisibleCount
  );
  const uploadedCount = videos.filter(video => video.already_uploaded).length;
  const hiddenUploadedCount = includeUploaded
    ? Math.max(0, uploadedCount - uploadedHistoryVisibleCount)
    : 0;

  if ($('workspacePending')) $('workspacePending').textContent = String(counts.pending || 0);
  if (info) {
    info.textContent = includeUploaded ? `${counts.pending || 0} ready · ${counts.uploaded || 0} uploaded or archived` : `${counts.pending || 0} ready for upload`;
    info.className = 'inline-status muted';
  }

  if (!videos.length) {
    box.innerHTML = '<div class="empty-workspace muted">No matching VOD files found.</div>';
    updateLocalUploadButton();
    return;
  }

  box.innerHTML = visibleVideos.map(renderLocalVideoCard).join('') + (
    hiddenUploadedCount
      ? `<button type="button" id="showMoreUploadedHistory" class="quiet-button show-more-upload-history">Show more · ${hiddenUploadedCount} older upload${hiddenUploadedCount === 1 ? '' : 's'}</button>`
      : ''
  );
  document.querySelectorAll('.localvideocheck').forEach(cb => cb.addEventListener('change', updateLocalUploadButton));
  document.querySelectorAll('.video-action').forEach(btn => btn.addEventListener('click', () => handleLocalVideoAction(btn.dataset.action, btn.dataset.path)));
  const showMore = $('showMoreUploadedHistory');
  if (showMore) showMore.addEventListener('click', () => {
    uploadedHistoryVisibleCount += UPLOADED_HISTORY_PAGE_SIZE;
    loadLocalVideos().catch(error => alert(error.message));
  });
  updateLocalUploadButton();
}

async function handleLocalVideoAction(action, path) {
  const video = localVideoByPath(path);
  if (!video) throw new Error('VOD data is no longer available. Refresh the file list.');

  if (action === 'upload') {
    await api('/api/youtube/upload-local', { method:'POST', body: JSON.stringify(localUploadRequestPayload([path])) });
    showToast('VOD added to the upload queue.');
    showPage('queue');
    await Promise.all([pollJobs(), loadLocalVideos()]);
    return;
  }

  if (action === 'prepare') {
    await saveCurrentSettingsSilently();
    const result = await api('/api/manual-upload/prepare-local', { method:'POST', body: JSON.stringify({ paths:[path] }) });
    if ((result.errors || []).length) throw new Error(result.errors[0].error || 'The VOD could not be prepared.');
    showToast('YouTube metadata prepared.');
    await loadLocalVideos();
    return;
  }

  if (action === 'copy-title') {
    await copyTextToClipboard(video.youtube_title || video.title, 'YouTube title');
    return;
  }

  if (action === 'copy-description') {
    await copyTextToClipboard(video.youtube_description || '', 'YouTube description');
    return;
  }

  if (action === 'mark') {
    if (!confirm(`Has the YouTube upload completed?

${video.name}

The VOD will be marked as manually uploaded.`)) return;
    await api('/api/local-video/mark-uploaded', { method:'POST', body: JSON.stringify({ path }) });
    showToast('Marked as manually uploaded.');
    await loadLocalVideos();
    return;
  }

  if (action === 'delete') {
    const ok = confirm(`Permanently delete this VOD and its matching TXT/JSON files?

${video.name}

The files will not be moved to the recycle bin or trash.`);
    if (!ok) return;
    const result = await api('/api/local-video/delete', {
      method:'POST',
      body: JSON.stringify({ path, confirm_name: video.name })
    });
    showToast(`Deleted. Freed ${result.freed_gb || 0} GB.`, 'warn');
    await loadLocalVideos();
  }
}

async function uploadSelectedLocalVideos() {
  await saveCurrentSettingsSilently();
  const paths = selectedLocalVideoPaths();
  if (!paths.length) {
    alert('No local VODs selected.');
    return;
  }
  const data = await api('/api/youtube/upload-local', { method:'POST', body: JSON.stringify(localUploadRequestPayload(paths)) });
  showPage('queue');
  await Promise.all([pollJobs(), loadLocalVideos()]);
  return data;
}


async function forceFixedStreamerPath() {
  const data = await api('/api/streamers/force-fixed-path', { method:'POST', body: JSON.stringify({}) });
  await loadState();
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = data.streamer_file || state.streamer_file_resolved || 'unknown';
  if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${data.count || 0} streamers loaded from the default path`;
  alert(
    'Default paths restored:\n\n' +
    'Streamer file:\n' + (data.streamer_file || 'unknown') +
    '\n\nArchive file:\n' + (data.archive_file || 'unknown') +
    '\n\nStreamers loaded: ' + (data.count || 0)
  );
}


async function repairStreamerNewlines() {
  const data = await api('/api/streamers/repair-newlines', { method:'POST', body: JSON.stringify({}) });
  await loadState();
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = data.streamer_file || state.streamer_file_resolved || 'unknown';
  if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${data.count || 0} streamers loaded after repair`;
  alert(
    'Streamer file repaired:\n' + (data.streamer_file || 'unknown') +
    '\n\nStreamers found: ' + (data.count || 0) +
    (data.had_literal_newlines ? '\n\nLiteral \\n sequences were converted to line breaks.' : '\n\nNo invalid literal newline sequences found.')
  );
}


async function checkStreamerFileStatus() {
  const data = await api('/api/streamers/status');
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = data.streamer_file || 'unknown';
  if ($('streamerFileStatus')) {
    $('streamerFileStatus').textContent = `${data.count || 0} streamers · File ${data.exists ? 'found' : 'missing'} · Writable: ${data.can_write ? 'yes' : 'no'}`;
  }
  alert(
    'Streamer file:\\n' + (data.streamer_file || 'unknown') +
    '\n\nStreamers found: ' + (data.count || 0) +
    '\nWritable: ' + (data.can_write ? 'yes' : 'no') +
    (data.write_error ? '\n\nError: ' + data.write_error : '') +
    (data.legacy_candidates && data.legacy_candidates.length ? '\n\nLegacy dashboard files (not used automatically):\\n' + data.legacy_candidates.join('\n') : '')
  );
  await loadState();
}

async function refreshYoutubeStatus() {
  const status = $('youtubeStatus');
  const refreshButton = $('youtubeLoadPlaylists');
  const playlist = $('youtubePlaylistId');
  const connectButton = $('youtubeConnect');
  try {
    const data = await api('/api/youtube/status');
    if (!data.google_libs_available) status.textContent = 'YouTube support is unavailable because the required libraries are not installed.';
    else if (data.connected) status.textContent = `Connected to ${data.channel_title || 'your YouTube channel'}.`;
    else if (!data.client_secret_exists) status.textContent = 'YouTube setup is incomplete. Add the client secret in Advanced YouTube options, then connect your account.';
    else status.textContent = 'YouTube is not connected. Connect your account to enable uploads.';
    status.className = `connection-status ${data.connected ? 'good' : 'muted'}`;
    if (refreshButton) {
      refreshButton.disabled = !data.connected;
      refreshButton.title = data.connected ? 'Reload playlists from YouTube' : 'Connect YouTube before loading playlists';
    }
    if (playlist) playlist.disabled = !data.connected;
    if (connectButton) connectButton.textContent = data.connected ? 'Reconnect YouTube' : 'Connect YouTube';
    return data;
  } catch (e) {
    status.textContent = 'YouTube status could not be checked. Try again in a moment.';
    status.className = 'connection-status bad';
    if (refreshButton) refreshButton.disabled = true;
    if (playlist) playlist.disabled = true;
    return null;
  }
}

async function loadYoutubePlaylists() {
  const status = $('streamerPlaylistStatus');
  try {
    const data = await api('/api/youtube/playlists');
    youtubePlaylistChoices = Array.isArray(data.playlists)
      ? data.playlists
      : [];
    renderGlobalPlaylistSelect();
    renderLocalUploadPlaylistSelect();
    renderStreamerEditor();
    if (status) {
      status.textContent = youtubePlaylistChoices.length
        ? `${youtubePlaylistChoices.length} YouTube playlist${youtubePlaylistChoices.length === 1 ? '' : 's'} available.`
        : 'No YouTube playlists are currently available. Existing streamer defaults are preserved.';
      status.className = 'field-message muted';
    }
    return data;
  } catch (error) {
    if (status) {
      status.textContent = 'Playlists could not be refreshed. Existing streamer defaults are preserved.';
      status.className = 'field-message warn';
    }
    throw error;
  }
}

function friendlyYoutubeConnectError(message) {
  const clean = String(message || '').replace(/\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception):\s*/g, '').trim();
  if (/not connected/i.test(clean)) return 'YouTube is not connected. Connect your account to enable uploads.';
  return clean || 'YouTube could not be connected. Check the setup and try again.';
}



async function saveYoutubeSettings() {
  const saved = await api('/api/settings', { method:'POST', body: JSON.stringify(gatherSettingsFromForm()) });
  await loadState();
  await refreshYoutubeStatus();
  await refreshDashboard();
  markSettingsSaved('saved: ' + (saved._saved_at || new Date().toLocaleTimeString()));
  alert('YouTube settings saved.\n\nFile: ' + (saved._settings_file || state.settings_file || 'unknown'));
}

$('presetToday').addEventListener('click', () => setDateRange(1));
$('preset7').addEventListener('click', () => setDateRange(7));
$('preset30').addEventListener('click', () => setDateRange(30));
$('streamerMode').addEventListener('change', () => {
  const mode = $('streamerMode').value;
  $('singleStreamerBox').classList.toggle('hidden', mode !== 'one');
  $('searchStreamerToggleCard').classList.toggle('hidden', mode === 'one' || mode === 'all');
});
$('searchStreamersAll').addEventListener('click', () => setAllSearchStreamers(true));
$('searchStreamersNone').addEventListener('click', () => setAllSearchStreamers(false));
$('searchBtn').addEventListener('click', () => searchVods().catch(e => alert(e.message)));
$('checkAll').addEventListener('change', e => { document.querySelectorAll('.rowcheck').forEach(cb => cb.checked = e.target.checked); refreshSelectionState(); });
$('downloadSelected').addEventListener('click', () => downloadSelectedWithConfirm().catch(e => alert(e.message)));
$('selectNewResults').addEventListener('click', () => selectNewResults());
$('clearResultsSelection').addEventListener('click', () => clearResultsSelection());
$('singleDownload').addEventListener('click', () => startSingleVodDownload().catch(e => { setSingleVodStatus('Error: ' + e.message, 'bad'); alert('VOD download failed:\n\n' + e.message); }));
$('streamerAddButton').addEventListener('click', addStreamerFromInput);
$('streamerAddInput').addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addStreamerFromInput();
});
$('saveStreamers').addEventListener('click', async () => {
  const saved = await api('/api/streamers', { method:'POST', body: JSON.stringify({ streamers: $('streamersText').value, streamer_profiles:streamerProfileDraft }) });
  await loadState();
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = saved.streamer_file || state.streamer_file_resolved || 'unknown';
  if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${saved.count || 0} streamers saved`;
  refreshAutoRecorderStatus().catch(() => {});
  showToast(`${saved.count || 0} streamer${saved.count === 1 ? '' : 's'} saved.`);
});
$('autoRecorderEnabled').addEventListener('change', updateAutoRecorderSettingCopy);
$('saveSettings').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'settings'));
$('youtubeConnect').addEventListener('click', async () => {
  try {
    await api('/api/settings', { method:'POST', body: JSON.stringify(gatherSettingsFromForm()) });
    const data = await api('/api/youtube/connect', { method:'POST', body:'{}' });
    await loadState();
    await refreshYoutubeStatus();
    try { await loadYoutubePlaylists(); } catch {}
    const tokenPath = data.status && data.status.token_path ? '\n\nToken saved to:\n' + data.status.token_path : '';
    alert('YouTube connected' + (data.channel_title ? ': ' + data.channel_title : '.') + tokenPath);
  } catch (e) {
    await refreshYoutubeStatus().catch(()=>{});
    alert('YouTube connection failed:\n\n' + friendlyYoutubeConnectError(e.message));
  }
});
$('youtubeLoadPlaylists').addEventListener('click', () => loadYoutubePlaylists().then(() => alert('Playlists loaded.')).catch(e => alert(e.message)));
$('saveYoutubeSettings').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'youtube'));
$('saveYoutubeSettingsBottom').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'youtube'));
$('refreshLiveStatuses').addEventListener('click', () => refreshLiveStatuses().catch(() => {}));

setInterval(() => pollJobs().catch(() => {}), 5000);
setInterval(() => refreshAutoRecorderStatus().catch(() => {}), AUTO_RECORDER_STATUS_REFRESH_MS);
loadState().then(() => {
  setDateRange(7);
  showPage(localStorage.getItem('vodActivePage') || 'dashboard');
  refreshYoutubeStatus();
  refreshDashboard();
  refreshAutoRecorderStatus();
  loadYoutubePlaylists().catch(() => {});
}).catch(e => {
  console.error(e);
  if (window.vodShowPage) window.vodShowPage(localStorage.getItem('vodActivePage') || 'dashboard');
  const sub = document.getElementById('pageSubtitle');
  if (sub) sub.textContent = 'The app loaded, but status data could not be read: ' + e.message;
});


(function v14FinalNavigationBinding() {
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.nav-btn, .goto-page').forEach(function(btn) {
      btn.onclick = function(ev) {
        ev.preventDefault();
        const page = btn.dataset.page || 'dashboard';
        if (typeof showPage === 'function') showPage(page);
        else if (window.vodShowPage) window.vodShowPage(page);
        if (typeof refreshDashboard === 'function') refreshDashboard().catch(function(){});
        return false;
      };
    });
  });
})();


(function v15YoutubeSaveFallback() {
  document.addEventListener('DOMContentLoaded', function() {
    ['saveYoutubeSettings', 'saveYoutubeSettingsBottom'].forEach(function(id) {
      const btn = document.getElementById(id);
      if (btn) {
        btn.onclick = function(ev) {
          ev.preventDefault();
          if (typeof saveYoutubeSettings === 'function') {
            saveYoutubeSettings().catch(function(e) { alert(e.message); });
          }
          return false;
        };
      }
    });
  });
})();


(function v16LocalUploadsBinding() {
  document.addEventListener('DOMContentLoaded', function() {
    const refreshBtn = document.getElementById('refreshLocalVideos');
    if (refreshBtn) refreshBtn.onclick = function(ev) { ev.preventDefault(); loadLocalVideos().catch(e => alert(e.message)); };
    const uploadBtn = document.getElementById('uploadSelectedLocalVideos');
    if (uploadBtn) uploadBtn.onclick = function(ev) { ev.preventDefault(); uploadSelectedLocalVideos().catch(e => alert(e.message)); };
    const checkAll = document.getElementById('checkAllLocalVideos');
    if (checkAll) checkAll.onclick = function(ev) {
      ev.preventDefault();
      document.querySelectorAll('.localvideocheck').forEach(cb => cb.checked = true);
      updateLocalUploadButton();
    };
    const uncheckAll = document.getElementById('uncheckAllLocalVideos');
    if (uncheckAll) uncheckAll.onclick = function(ev) {
      ev.preventDefault();
      document.querySelectorAll('.localvideocheck').forEach(cb => cb.checked = false);
      updateLocalUploadButton();
    };
  });
})();

try { window.refreshDashboard = refreshDashboard; } catch(e) {}
try { window.refreshAutoRecorderStatus = refreshAutoRecorderStatus; } catch(e) {}

document.addEventListener('DOMContentLoaded', function(){ const b=document.getElementById('checkSettingsStatus'); if(b) b.onclick=function(e){ window.vodCheckSettingsStatus(e); return false; }; });

document.addEventListener('DOMContentLoaded', function(){ const b=document.getElementById('checkStreamerFile'); if(b) b.onclick=function(e){ e.preventDefault(); checkStreamerFileStatus().catch(err => alert(err.message)); return false; }; });


document.addEventListener('DOMContentLoaded', function() {
  const manualBtn = document.getElementById('resetManualFilenameTemplate');
  if (manualBtn) manualBtn.onclick = function(ev) {
    ev.preventDefault();
    $('manualUploadFilenameTemplate').value = MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE;
    alert('The final YouTube filename template was reset:\n\n' + MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE);
    return false;
  };

  const ytdlpBtn = document.getElementById('resetYtdlpOutputTemplate');
  if (ytdlpBtn) ytdlpBtn.onclick = function(ev) {
    ev.preventDefault();
    $('outputTemplate').value = YTDLP_DEFAULT_OUTPUT_TEMPLATE;
    alert('The technical yt-dlp output template was reset:\n\n' + YTDLP_DEFAULT_OUTPUT_TEMPLATE);
    return false;
  };
});

document.addEventListener('DOMContentLoaded', function(){
  const b=document.getElementById('forceFixedStreamerPath');
  if(b) b.onclick=function(e){ e.preventDefault(); forceFixedStreamerPath().catch(err => alert(err.message)); return false; };
});

document.addEventListener('DOMContentLoaded', function(){
  const b=document.getElementById('repairStreamerNewlines');
  if(b) b.onclick=function(e){ e.preventDefault(); repairStreamerNewlines().catch(err => alert(err.message)); return false; };
});


document.addEventListener('DOMContentLoaded', function() {
  const stickyDownload = document.getElementById('stickyDownloadSelected');
  if (stickyDownload) stickyDownload.onclick = function(ev) {
    ev.preventDefault();
    downloadSelectedWithConfirm().catch(e => alert(e.message));
    return false;
  };

  const stickyClear = document.getElementById('stickyClearSelection');
  if (stickyClear) stickyClear.onclick = function(ev) {
    ev.preventDefault();
    clearResultsSelection();
    return false;
  };

  const includeUploaded = document.getElementById('includeUploadedLocalVideos');
  if (includeUploaded) includeUploaded.onchange = function() {
    uploadedHistoryVisibleCount = UPLOADED_HISTORY_PAGE_SIZE;
    loadLocalVideos().catch(e => alert(e.message));
  };
});


// sticky bar navigation safety
document.addEventListener('click', function(event) {
  const navButton = event.target.closest('[data-page]');
  if (!navButton) return;
  const targetPage = navButton.dataset.page || '';
  document.body.dataset.activePage = targetPage;
  const bar = document.getElementById('searchSelectionBar');
  if (bar && targetPage !== 'search') {
    bar.classList.add('hidden');
    bar.style.display = 'none';
    bar.setAttribute('aria-hidden', 'true');
  }
});


// sticky bar initial state
document.addEventListener('DOMContentLoaded', function() {
  if (!document.body.dataset.activePage) {
    const active = document.querySelector('.page.active');
    document.body.dataset.activePage = active ? active.id.replace('page-', '') : 'dashboard';
  }
  if (typeof refreshSelectionState === 'function') refreshSelectionState();
});

document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.settings-tab').forEach(tab => {
    tab.addEventListener('click', () => showSettingsTab(tab.dataset.settingsTab));
  });
  let initialTab = 'general';
  try { initialTab = localStorage.getItem('vodSettingsTab') || 'general'; } catch {}
  showSettingsTab(initialTab);

  const toggle = document.getElementById('mobileNavToggle');
  const sidebar = document.getElementById('appSidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      const open = sidebar.classList.toggle('mobile-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.textContent = open ? 'Close' : 'Menu';
    });
    sidebar.querySelectorAll('.nav-btn').forEach(btn => btn.addEventListener('click', () => {
      sidebar.classList.remove('mobile-open');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.textContent = 'Menu';
    }));
  }
});

document.addEventListener('DOMContentLoaded', function() {
  const clearButton = document.getElementById('clearCompletedJobs');
  const dialog = document.getElementById('clearCompletedDialog');
  const confirmButton = document.getElementById('confirmClearCompletedJobs');
  if (clearButton && dialog) clearButton.addEventListener('click', () => {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  });
  if (confirmButton && dialog) confirmButton.addEventListener('click', async () => {
    confirmButton.disabled = true;
    try {
      const result = await api('/api/jobs/clear-completed', {method:'POST', body:'{}'});
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
      showToast(`${result.cleared_jobs || 0} completed job${result.cleared_jobs === 1 ? '' : 's'} removed from Dashboard history.`);
      await pollJobs();
    } catch (error) {
      showToast(friendlyQueueActionError(error), 'bad');
    } finally {
      confirmButton.disabled = false;
    }
  });
});
