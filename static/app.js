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
  setSingleVodStatus('Download started. Job ID: ' + data.job_id, 'good');
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
      alert((scope === 'youtube' ? 'YouTube settings' : 'Settings') + ' saved.\n\nFile: ' + (saved._settings_file || 'unknown'));
      return saved;
    } catch (e) {
      console.error(e);
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
    dashboard: ['Dashboard', 'Downloads, YouTube, and storage at a glance.'],
    search: ['VOD Search', 'Search by date range and streamer, or download a single VOD.'],
    queue: ['Queue and Log', 'Active downloads and YouTube uploads.'],
    youtube: ['YouTube', 'Connection, playlist, upload mode, and metadata.'],
  localuploads: ['Prepare for YouTube', 'Prepare manual uploads, track status, and clean up local VODs.'],
    streamers: ['Streamers', 'Manage frequently used Twitch channels.'],
    settings: ['Settings', 'Paths and core yt-dlp settings.']
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
let autoExpandJobDetails = localStorage.getItem('vodJobAutoExpand') === '1';

const $ = (id) => document.getElementById(id);

const YTDLP_DEFAULT_OUTPUT_TEMPLATE = '%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s';
const MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = '{date_de} - {streamer} - {title}';

const pageMeta = {
  dashboard: ['Dashboard', 'Downloads, YouTube, and storage at a glance.'],
  search: ['VOD Search', 'Search by date range and streamer, or download a single VOD.'],
  queue: ['Queue and Log', 'Active downloads and YouTube uploads.'],
  youtube: ['YouTube', 'Connection, playlist, upload mode, and metadata.'],
  localuploads: ['Prepare for YouTube', 'Prepare manual uploads, track status, and clean up local VODs.'],
  streamers: ['Streamers', 'Manage frequently used Twitch channels.'],
  settings: ['Settings', 'Paths and core yt-dlp settings.']
};

function showPage(name) {
  document.body.dataset.activePage = name;
  const selectionBar = document.getElementById('searchSelectionBar');
  if (selectionBar && name !== 'search') {
    selectionBar.classList.add('hidden');
    selectionBar.style.display = 'none';
    selectionBar.setAttribute('aria-hidden', 'true');
  }
  window.vodShowPage(name);
  refreshDashboard().catch(() => {});
  if (name === 'localuploads' && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});
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
    throw new Error(detail);
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
  $('streamersText').value = state.streamers.join('\n');
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
  $('youtubeEnabled').checked = !!state.settings.youtube_enabled;
  $('youtubeAutoUpload').checked = !!state.settings.youtube_auto_upload;
  $('moveUploadedVods').checked = state.settings.move_uploaded_vods !== false;
  $('uploadedVodsFolder').value = state.settings.uploaded_vods_folder || '';
  $('youtubePrivacyStatus').value = state.settings.youtube_privacy_status || 'private';
  $('youtubeClientSecretFile').value = state.settings.youtube_client_secret_file || '';
  $('youtubeTokenFile').value = state.settings.youtube_token_file || '';
  $('youtubeDescription').value = state.settings.youtube_description || '';
  $('youtubeTags').value = state.settings.youtube_tags || '';
  $('youtubeCategoryId').value = state.settings.youtube_category_id || '20';
  $('youtubeTitleTemplate').value = state.settings.youtube_title_template || '{streamer} VOD - {date_de} - {title}';
  $('youtubeDescriptionTemplate').value = state.settings.youtube_description_template || '';
  $('manualUploadFilenameTemplate').value = state.settings.manual_upload_filename_template || MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE;
  $('manualUploadPrepareEnabled').checked = state.settings.manual_upload_prepare_enabled !== false;
  $('manualUploadRenameVideo').checked = state.settings.manual_upload_rename_video !== false;
  $('manualUploadWriteDescription').checked = state.settings.manual_upload_write_description !== false;
  $('manualUploadWriteMetadataJson').checked = state.settings.manual_upload_write_metadata_json !== false;
  $('youtubeChunkSizeMb').value = String(state.settings.youtube_chunk_size_mb || 64);
  $('youtubeUploadMode').value = state.settings.youtube_upload_mode || 'stable';
  $('singleStreamer').innerHTML = state.streamers.map(s => `<option>${escapeHtml(s)}</option>`).join('');
  renderSearchStreamerCheckboxes();
  $('jobAutoExpand').checked = autoExpandJobDetails;
  updateJobDetailButtonLabel();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

async function loadState() {
  state = await api('/api/state');
  renderState();
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
  const errHtml = errors && errors.length ? errors.map(e => `<div class="errorbox"><b>${escapeHtml(e.streamer)}</b>: ${escapeHtml(e.error)}</div>`).join('') : '';
  const dbgHtml = debug && debug.length ? `<div class="debugbox">${debug.map(d => `${escapeHtml(d.streamer)}: ${d.kept}/${d.deduped || d.found_raw} shown · raw source results: ${d.found_raw} · unknown date: ${d.unknown_dates} · outside date range: ${d.skipped_by_date} · live/upcoming filtered: ${d.skipped_live || 0} · non-VOD filtered: ${d.skipped_nonvod || 0}`).join('<br>')}</div>` : '';
  $('searchErrors').innerHTML = errHtml + dbgHtml;
  const body = $('resultsBody');
  if (!lastResults.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No matching VODs found.<br><span class="small">Try increasing the search depth or expanding the date range. The diagnostics above show whether results were filtered.</span></td></tr>`;
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
    rows.push(`<tr class="streamer-group-row"><td colspan="6"><div class="streamer-group-head"><div><strong>${escapeHtml(streamer)}</strong><span>${items.length} VOD(s), ${openCount} new/pending</span></div><div><button type="button" class="group-select" data-streamer="${escapeHtml(streamer)}">Select All</button><button type="button" class="group-clear" data-streamer="${escapeHtml(streamer)}">Clear</button></div></div></td></tr>`);
    items.forEach(r => {
      rows.push(`
        <tr>
          <td><input class="rowcheck" type="checkbox" data-url="${escapeHtml(r.url)}" data-streamer="${escapeHtml(r.streamer)}" data-already-downloaded="${r.already_downloaded ? 'true' : 'false'}"></td>
          <td>${escapeHtml(r.date)}</td>
          <td>${escapeHtml(r.streamer)}</td>
          <td>${escapeHtml(r.title)}</td>
          <td class="${r.already_downloaded ? 'good' : ''}">${r.already_downloaded ? 'Already in Archive' : 'New/Pending'}${r.outside_range ? '<br><span class="warn">Outside Date Range</span>' : ''}</td>
          <td><a href="${escapeHtml(r.url)}" target="_blank">Open</a></td>
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
  const ids = Object.keys(jobOpenState);
  const anyClosed = ids.some(v => !jobOpenState[v]);
  $('toggleJobDetails').textContent = anyClosed || !ids.length ? 'Show All Details' : 'Hide All Details';
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
  const current = [activeJob.label, info.currentItem].filter(Boolean).join(' · ');
  if ($('queueCurrent')) $('queueCurrent').textContent = current || activeJob.label;
  if ($('queueEta')) {
    $('queueEta').textContent = [
      info.downloadProgress !== null ? `${info.downloadProgress}%` : '',
      info.downloadSpeed,
      info.eta ? `about ${info.eta} remaining` : ''
    ].filter(Boolean).join(' · ');
  }
}


async function pollJobs() {
  collectOpenStates();
  const data = await api('/api/jobs');
  const box = $('jobs');
  updateQueueSummary(data.jobs || []);
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


function setSetupItemState(id, stateName, text) {
  const item = document.getElementById(id);
  if (!item) return;
  item.classList.remove('good', 'warn', 'bad');
  item.classList.add(stateName);
  const textEl = document.getElementById(id + 'Text');
  if (textEl) textEl.textContent = text;
}

function updateCommandCenter(data) {
  if (!state || !state.settings) return;
  const yt = data.youtube || {};
  const disk = data.disk || {};
  const streamerCount = (state.streamers || []).length;
  let ready = 0;

  if (streamerCount > 0) {
    ready++;
    setSetupItemState('setupStreamers', 'good', `${streamerCount} streamers loaded.`);
  } else {
    setSetupItemState('setupStreamers', 'bad', 'No streamers loaded. Check the streamer file or save the list.');
  }

  if (yt.connected) {
    ready++;
    setSetupItemState('setupYoutube', 'good', `Connected${yt.channel_title ? ': ' + yt.channel_title : ''}.`);
  } else if (yt.client_secret_exists) {
    setSetupItemState('setupYoutube', 'warn', 'client_secret.json found, but YouTube is not connected.');
  } else {
    setSetupItemState('setupYoutube', 'bad', 'client_secret.json is missing or its path is incorrect.');
  }

  if (disk.ok && disk.free_gb >= 50) {
    ready++;
    setSetupItemState('setupStorage', 'good', `${disk.free_gb} GB free.`);
  } else if (disk.ok) {
    setSetupItemState('setupStorage', 'warn', `${disk.free_gb} GB free. Large VODs may run out of space.`);
  } else {
    setSetupItemState('setupStorage', 'warn', 'Storage information is unavailable.');
  }

  if (data.jobs_failed > 0) {
    setSetupItemState('setupWorkflow', 'bad', `${data.jobs_failed} job(s) failed. Check the queue.`);
  } else if (data.jobs_active > 0) {
    ready++;
    setSetupItemState('setupWorkflow', 'good', `${data.jobs_active} job(s) running.`);
  } else {
    ready++;
    setSetupItemState('setupWorkflow', 'good', 'Ready to search or download a single VOD.');
  }

  const score = document.getElementById('setupScore');
  if (score) score.textContent = `${ready}/4 ready`;

  const next = decideNextAction(data, yt, disk, streamerCount);
  const title = document.getElementById('nextActionTitle');
  const text = document.getElementById('nextActionText');
  const btn = document.getElementById('nextActionButton');
  if (title) title.textContent = next.title;
  if (text) text.textContent = next.text;
  if (btn) {
    btn.textContent = next.button;
    btn.dataset.page = next.page;
    btn.onclick = () => showPage(next.page);
  }
}

function decideNextAction(data, yt, disk, streamerCount) {
  if (streamerCount === 0) {
    return {
      title: 'Add Streamers First',
      text: 'VOD Search needs a streamer list. Open Streamers, check the file, and save the list.',
      button: 'Open Streamers',
      page: 'streamers'
    };
  }
  if (data.jobs_failed > 0) {
    return {
      title: 'Review Failed Jobs',
      text: 'One or more jobs failed. Open the queue, expand the details, and check the latest error.',
      button: 'Open Queue',
      page: 'queue'
    };
  }
  if (!yt.connected) {
    return {
      title: yt.client_secret_exists ? 'Connect YouTube' : 'Add client_secret.json',
      text: yt.client_secret_exists ? 'The client secret is available. Connect your YouTube channel now.' : 'Add client_secret.json to the dashboard data folder, then connect YouTube.',
      button: 'Open YouTube',
      page: 'youtube'
    };
  }
  if (disk.ok && disk.free_gb < 50) {
    return {
      title: 'Check Available Storage',
      text: 'Large VODs may fail when storage is low. Check the download path or upload and clean up local VODs.',
      button: 'Local VODs',
      page: 'localuploads'
    };
  }
  return {
    title: 'Ready for the Next Download',
    text: 'Search VODs by date range or enter a VOD link for a quick download.',
    button: 'Search VODs',
    page: 'search'
  };
}


async function refreshDashboard() {
  try {
    const data = await api('/api/dashboard');
    const yt = data.youtube || {};
    const disk = data.disk || {};
    const ytText = yt.connected ? `Connected: ${yt.channel_title || 'Channel'}` : (yt.client_secret_exists ? 'Not Connected' : 'Missing client_secret');
    const jobsText = `${data.jobs_active} active · ${data.jobs_finished} completed · ${data.jobs_failed} errors`;
    const diskText = disk.ok ? `${disk.free_gb} GB free` : 'Unavailable';
    const uploadText = `${data.upload_mode} · ${data.upload_chunk_mb} MB`;
    $('statusYoutube').textContent = 'YouTube: ' + ytText;
    $('statusJobs').textContent = 'Jobs: ' + jobsText;
    $('statusDisk').textContent = 'Storage: ' + diskText;
    $('statusUploadMode').textContent = 'Upload: ' + uploadText;
    $('dashYoutube').textContent = ytText;
    $('dashYoutubeHint').textContent = yt.connected ? 'Uploads are available.' : 'Connect YouTube or check client_secret.json.';
    $('dashJobs').textContent = jobsText;
    $('dashDisk').textContent = diskText;
    $('dashUploadMode').textContent = uploadText;
    const hints = [];
    if (!yt.connected) hints.push(['YouTube Not Connected', 'Connect YouTube before enabling automatic uploads.', 'youtube']);
    if (data.jobs_failed > 0) hints.push(['Failed Jobs', 'Open queue details and review the errors.', 'queue']);
    if (disk.ok && disk.free_gb < 50) hints.push(['Low Storage', 'Large VOD downloads may fail. Check the download folder and available storage.', 'settings']);
    if (!hints.length) hints.push(['Ready', 'You can search VODs or start a quick download.', 'search']);
    $('dashboardHints').innerHTML = hints.map(h => `<div class="hint-item"><div><strong>${escapeHtml(h[0])}</strong><span>${escapeHtml(h[1])}</span></div><button class="goto-page" data-page="${escapeHtml(h[2])}">Open</button></div>`).join('');
    document.querySelectorAll('.goto-page').forEach(btn => btn.onclick = () => showPage(btn.dataset.page));
    updateCommandCenter(data);
  } catch (e) {
    $('statusYoutube').textContent = 'Status: Error';
    const nextTitle = document.getElementById('nextActionTitle');
    const nextText = document.getElementById('nextActionText');
    if (nextTitle) nextTitle.textContent = 'Command Center: Status Error';
    if (nextText) nextText.textContent = e.message;
  }
}


let localVideoCache = new Map();

function selectedLocalVideoPaths() {
  return [...document.querySelectorAll('.localvideocheck:checked')].map(cb => cb.dataset.path);
}

function updateLocalUploadButton() {
  const selected = selectedLocalVideoPaths().length;
  const uploadBtn = $('uploadSelectedLocalVideos');
  const prepareBtn = $('prepareSelectedLocalVideos');
  if (uploadBtn) uploadBtn.disabled = selected === 0;
  if (prepareBtn) prepareBtn.disabled = selected === 0;
}

function workspaceStatusClass(video) {
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
  const statusClassName = workspaceStatusClass(v);
  const marked = v.manually_uploaded || v.dashboard_uploaded;
  const actionMark = marked
    ? `<button type="button" class="video-action" data-action="move" data-path="${escapeHtml(v.path)}">Move to _hochgeladen</button>`
    : `<button type="button" class="video-action" data-action="mark" data-path="${escapeHtml(v.path)}">Mark as Uploaded</button>`;

  return `
    <article class="video-workspace-card ${marked ? 'is-uploaded' : ''}" data-video-path="${escapeHtml(v.path)}">
      <div class="video-card-top">
        <label class="video-select"><input class="localvideocheck" type="checkbox" data-path="${escapeHtml(v.path)}" ${v.already_uploaded ? '' : 'checked'}><span>Select</span></label>
        <span class="pill ${statusClassName}">${escapeHtml(workspaceStatusLabel(v))}</span>
      </div>

      <div class="video-file-block">
        <strong title="${escapeHtml(v.name)}">${escapeHtml(v.name)}</strong>
        <span>${escapeHtml(v.streamer || 'Unknown')} · ${escapeHtml(v.date_de || 'Unknown Date')} · ${escapeHtml(v.size_gb)} GB</span>
        <small title="${escapeHtml(v.folder)}">${escapeHtml(v.folder)}</small>
      </div>

      <div class="video-title-preview">
        <span>YouTube Title</span>
        <strong>${escapeHtml(v.youtube_title || v.title || '')}</strong>
      </div>

      <div class="video-primary-actions">
        <button type="button" class="primary video-action" data-action="workflow" data-path="${escapeHtml(v.path)}">YouTube Studio + Show in Folder</button>
        <button type="button" class="video-action" data-action="explorer" data-path="${escapeHtml(v.path)}">Show in Folder</button>
      </div>

      <div class="video-copy-actions">
        <button type="button" class="video-action" data-action="copy-title" data-path="${escapeHtml(v.path)}">Copy Title</button>
        <button type="button" class="video-action" data-action="copy-description" data-path="${escapeHtml(v.path)}">Copy Description</button>
        <button type="button" class="video-action" data-action="open-txt" data-path="${escapeHtml(v.path)}" ${v.description_file_exists ? '' : 'disabled'}>Open TXT</button>
      </div>

      <div class="video-cleanup-actions">
        ${actionMark}
        <button type="button" class="danger-outline video-action" data-action="delete" data-path="${escapeHtml(v.path)}">Delete Permanently</button>
      </div>
    </article>`;
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

  if ($('workspacePending')) $('workspacePending').textContent = String(counts.pending || 0);
  if ($('workspaceUploaded')) $('workspaceUploaded').textContent = String(counts.uploaded || 0);
  if ($('workspaceTotal')) $('workspaceTotal').textContent = String(counts.total || 0);
  if ($('workspaceSize')) $('workspaceSize').textContent = `${counts.size_gb || 0} GB`;

  if (info) {
    info.textContent = `${videos.length} VOD file(s) shown · Media root: ${data.root}`;
    info.className = 'inline-status muted';
  }

  if (!videos.length) {
    box.innerHTML = '<div class="empty-workspace muted">No matching VOD files found.</div>';
    updateLocalUploadButton();
    return;
  }

  box.innerHTML = videos.map(renderLocalVideoCard).join('');
  document.querySelectorAll('.localvideocheck').forEach(cb => cb.addEventListener('change', updateLocalUploadButton));
  document.querySelectorAll('.video-action').forEach(btn => btn.addEventListener('click', () => handleLocalVideoAction(btn.dataset.action, btn.dataset.path)));
  updateLocalUploadButton();
}

async function handleLocalVideoAction(action, path) {
  const video = localVideoByPath(path);
  if (!video) throw new Error('VOD data is no longer available. Refresh the file list.');

  if (action === 'explorer') {
    await api('/api/local-video/open', { method:'POST', body: JSON.stringify({ path, mode:'select' }) });
    showToast('VOD selected in its folder.');
    return;
  }

  if (action === 'open-txt') {
    await api('/api/local-video/open', { method:'POST', body: JSON.stringify({ path, mode:'description' }) });
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

  if (action === 'workflow') {
    // Sofort öffnen, damit Browser den Tab nicht als Popup blockiert.
    window.open('https://studio.youtube.com', '_blank', 'noopener');
    await copyTextToClipboard(video.youtube_title || video.title, 'YouTube title');
    await api('/api/local-video/open', { method:'POST', body: JSON.stringify({ path, mode:'select' }) });
    showToast('Opened YouTube Studio, selected the VOD in its folder, and copied the title. Upload remains manual.');
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

  if (action === 'move') {
    if (!confirm(`Move this VOD to the _hochgeladen folder?

${video.name}

The original will no longer remain in its current folder.`)) return;
    const result = await api('/api/local-video/move-uploaded', { method:'POST', body: JSON.stringify({ path }) });
    if (!result.source_removed) throw new Error('The source was not removed, so the move is considered unsuccessful.');
    showToast('VOD moved and source removed.');
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

async function prepareSelectedLocalVideos() {
  await saveCurrentSettingsSilently();
  const paths = selectedLocalVideoPaths();
  if (!paths.length) {
    alert('No local VODs selected.');
    return;
  }
  const data = await api('/api/manual-upload/prepare-local', { method:'POST', body: JSON.stringify({ paths }) });
  showToast(`${(data.prepared || []).length} VOD file(s) prepared for YouTube.`);
  if ((data.errors || []).length) alert(`${(data.errors || []).length} VOD file(s) could not be prepared.`);
  await loadLocalVideos();
  return data;
}

async function uploadSelectedLocalVideos() {
  await saveCurrentSettingsSilently();
  const paths = selectedLocalVideoPaths();
  if (!paths.length) {
    alert('No local VODs selected.');
    return;
  }
  const data = await api('/api/youtube/upload-local', { method:'POST', body: JSON.stringify({ paths }) });
  showPage('queue');
  await pollJobs();
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
  try {
    const data = await api('/api/youtube/status');
    let text = 'YouTube: ';
    if (!data.google_libs_available) text += 'Required libraries are unavailable.';
    else if (data.connected) text += `Connected to ${data.channel_title || 'Channel'}`;
    else if (!data.client_secret_exists) text += 'client_secret.json is missing.';
    else if (!data.token_exists) text += 'Not connected.';
    else text += data.error ? `Error: ${data.error}` : 'Not connected.';
    $('youtubeStatus').textContent = text;
  } catch (e) {
    $('youtubeStatus').textContent = 'YouTube Error: ' + e.message;
  }
}

async function loadYoutubePlaylists() {
  const data = await api('/api/youtube/playlists');
  const current = state.settings.youtube_playlist_id || '';
  $('youtubePlaylistId').innerHTML = '<option value="">No Playlist</option>' + data.playlists.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.title)}</option>`).join('');
  $('youtubePlaylistId').value = current;
  return data;
}



async function saveYoutubeSettings() {
  const saved = await api('/api/settings', { method:'POST', body: JSON.stringify(gatherSettingsFromForm()) });
  await loadState();
  await refreshYoutubeStatus();
  await refreshDashboard();
  markSettingsSaved('saved: ' + (saved._saved_at || new Date().toLocaleTimeString()));
  alert('YouTube settings saved.\n\nFile: ' + (saved._settings_file || state.settings_file || 'unknown'));
}

async function previewYoutubeMetadata() {
  const path = $('youtubePreviewPath').value.trim();
  if (!path) {
    alert('Enter a local VOD path to preview.');
    return;
  }
  await saveCurrentSettingsSilently();
  const data = await api('/api/youtube/preview-file', { method:'POST', body: JSON.stringify({ path }) });
  const m = data.meta || {};
  $('youtubePreviewBox').classList.remove('muted');
  $('youtubePreviewBox').innerHTML = `
    <div><span>Title</span><strong>${escapeHtml(data.title || '')}</strong></div>
    <div><span>Date</span><strong>${escapeHtml(m.date_de || m.date || 'unknown')}</strong></div>
    <div><span>Streamer</span><strong>${escapeHtml(m.streamer || 'unknown')}</strong></div>
    <div><span>VOD ID</span><strong>${escapeHtml(m.vod_id || 'unknown')}</strong></div>
    <div><span>Original</span><strong>${m.url ? `<a href="${escapeHtml(m.url)}" target="_blank">${escapeHtml(m.url)}</a>` : 'unknown'}</strong></div>
    <div class="wide-preview"><span>Description</span><pre>${escapeHtml(data.description || '')}</pre></div>
  `;
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
$('validateSingleVod').addEventListener('click', () => validateSingleVodLink(true).catch(e => { setSingleVodStatus('Error: ' + e.message, 'bad'); alert(e.message); }));
$('singleDownload').addEventListener('click', () => startSingleVodDownload().catch(e => { setSingleVodStatus('Error: ' + e.message, 'bad'); alert('VOD download failed:\n\n' + e.message); }));
$('saveStreamers').addEventListener('click', async () => {
  const saved = await api('/api/streamers', { method:'POST', body: JSON.stringify({ streamers: $('streamersText').value }) });
  await loadState();
  if ($('streamerFileInfo')) $('streamerFileInfo').textContent = saved.streamer_file || state.streamer_file_resolved || 'unknown';
  if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${saved.count || 0} streamers saved`;
  alert('Streamers saved.\n\nFile: ' + (saved.streamer_file || 'unknown') + '\nCount: ' + (saved.count || 0));
});
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
    alert('YouTube connection failed:\n\n' + e.message);
  }
});
$('youtubeLoadPlaylists').addEventListener('click', () => loadYoutubePlaylists().then(() => alert('Playlists loaded.')).catch(e => alert(e.message)));
$('saveYoutubeSettings').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'youtube'));
$('saveYoutubeSettingsBottom').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'youtube'));
$('openFolder').addEventListener('click', () => api('/api/open-folder', { method:'POST', body:'{}' }).catch(e => alert(e.message)));
$('youtubePreviewBtn').addEventListener('click', () => previewYoutubeMetadata().catch(e => alert(e.message)));
$('jobAutoExpand').addEventListener('change', e => {
  autoExpandJobDetails = !!e.target.checked;
  localStorage.setItem('vodJobAutoExpand', autoExpandJobDetails ? '1' : '0');
  Object.keys(jobOpenState).forEach(id => jobOpenState[id] = autoExpandJobDetails);
  pollJobs().catch(() => {});
});
$('toggleJobDetails').addEventListener('click', () => {
  const ids = Object.keys(jobOpenState);
  const shouldOpen = ids.some(id => !jobOpenState[id]) || !ids.length;
  ids.forEach(id => jobOpenState[id] = shouldOpen);
  pollJobs().catch(() => {});
});

setInterval(() => pollJobs().catch(() => {}), 2000);
loadState().then(() => {
  setDateRange(7);
  showPage(localStorage.getItem('vodActivePage') || 'dashboard');
  refreshYoutubeStatus();
  refreshDashboard();
  try { loadYoutubePlaylists(); } catch {}
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
        if (window.vodShowPage) window.vodShowPage(page);
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
    const prepareBtn = document.getElementById('prepareSelectedLocalVideos');
    if (prepareBtn) prepareBtn.onclick = function(ev) { ev.preventDefault(); prepareSelectedLocalVideos().catch(e => alert(e.message)); };
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

  const studio = document.getElementById('openYoutubeStudio');
  if (studio) studio.onclick = function(ev) {
    ev.preventDefault();
    window.open('https://studio.youtube.com', '_blank', 'noopener');
    return false;
  };

  const includeUploaded = document.getElementById('includeUploadedLocalVideos');
  if (includeUploaded) includeUploaded.onchange = function() {
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
