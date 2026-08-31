const VOD_CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || '';

const TOAST_VARIANTS = new Set(['success', 'info', 'warning', 'error']);
const LEGACY_TOAST_VARIANTS = {good:'success', warn:'warning', bad:'error'};
const TOAST_TIMEOUTS = {success:3200, info:3600, warning:5000, error:6500};

function toastOptions(options) {
  const raw = typeof options === 'string' ? {variant:options} : (options || {});
  const variant = LEGACY_TOAST_VARIANTS[raw.variant || raw.kind] || raw.variant || raw.kind || 'success';
  return {
    variant: TOAST_VARIANTS.has(variant) ? variant : 'success',
    duration: Number.isFinite(Number(raw.duration)) ? Math.max(0, Number(raw.duration)) : null,
  };
}

function dismissToast(toast) {
  if (!toast) return;
  const timer = Number(toast.dataset.toastTimer);
  if (timer) clearTimeout(timer);
  toast.remove();
}

function showToast(message, options={}) {
  const container = document.getElementById('appToastContainer');
  if (!container) return null;
  const {variant, duration} = toastOptions(options);
  const text = String(message ?? '');
  const key = `${variant}:${text}`;
  const existing = [...container.querySelectorAll('.app-toast')].find(toast => toast.dataset.toastKey === key);
  if (existing) {
    dismissToast(existing);
  }
  const toast = document.createElement('div');
  toast.className = `app-toast is-${variant}`;
  toast.dataset.toastKey = key;
  toast.setAttribute('role', variant === 'error' || variant === 'warning' ? 'alert' : 'status');
  toast.setAttribute('aria-live', variant === 'error' || variant === 'warning' ? 'assertive' : 'polite');
  const copy = document.createElement('span');
  copy.className = 'app-toast-message';
  copy.textContent = text;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'app-toast-dismiss';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.textContent = '×';
  close.addEventListener('click', () => dismissToast(toast));
  toast.append(copy, close);
  container.appendChild(toast);
  const timeout = duration === null ? TOAST_TIMEOUTS[variant] : duration;
  if (timeout > 0) toast.dataset.toastTimer = String(setTimeout(() => dismissToast(toast), timeout));
  return toast;
}

let activeConfirmation = null;

function confirmationFocusableElements(dialog) {
  return [...dialog.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter(element => !element.hidden);
}

function restoreConfirmationFocus(trigger) {
  if (!trigger || typeof trigger.focus !== 'function' || !document.contains(trigger)) return;
  try { trigger.focus({preventScroll:true}); } catch { trigger.focus(); }
}

function confirmAction(options={}) {
  const dialog = document.getElementById('appConfirmDialog');
  if (!dialog || activeConfirmation) return Promise.resolve(false);
  const title = document.getElementById('appConfirmDialogTitle');
  const message = document.getElementById('appConfirmDialogDescription');
  const cancelButton = document.getElementById('appConfirmDialogCancel');
  const confirmButton = document.getElementById('appConfirmDialogAccept');
  if (!title || !message || !cancelButton || !confirmButton) return Promise.resolve(false);

  const variant = options.variant === 'danger' ? 'danger' : 'default';
  const trigger = options.trigger || document.activeElement;
  title.textContent = String(options.title || 'Confirm action');
  message.textContent = String(options.message || '');
  cancelButton.textContent = String(options.cancelLabel || 'Cancel');
  confirmButton.textContent = String(options.confirmLabel || 'Confirm');
  confirmButton.className = variant === 'danger' ? 'danger-outline' : 'primary';
  dialog.classList.toggle('is-danger', variant === 'danger');

  return new Promise(resolve => {
    let settled = false;
    const finish = result => {
      if (settled) return;
      settled = true;
      dialog.removeEventListener('cancel', onCancel);
      dialog.removeEventListener('close', onNativeClose);
      dialog.removeEventListener('click', onBackdropClick);
      dialog.removeEventListener('keydown', onKeydown);
      cancelButton.removeEventListener('click', onCancelClick);
      confirmButton.removeEventListener('click', onConfirmClick);
      activeConfirmation = null;
      if (dialog.open) {
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
      }
      restoreConfirmationFocus(trigger);
      resolve(Boolean(result));
    };
    const onCancel = event => { event.preventDefault(); finish(false); };
    const onNativeClose = () => finish(false);
    const onCancelClick = () => finish(false);
    const onConfirmClick = () => finish(true);
    const onBackdropClick = event => { if (event.target === dialog) finish(false); };
    const onKeydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        finish(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = confirmationFocusableElements(dialog);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    activeConfirmation = {dialog, resolve:finish};
    dialog.addEventListener('cancel', onCancel);
    dialog.addEventListener('close', onNativeClose);
    dialog.addEventListener('click', onBackdropClick);
    dialog.addEventListener('keydown', onKeydown);
    cancelButton.addEventListener('click', onCancelClick);
    confirmButton.addEventListener('click', onConfirmClick);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    // Cancellation is the safe default; especially important for destructive actions.
    cancelButton.focus();
  });
}

const pendingButtonActions = new WeakMap();

function withButtonPending(button, options={}, action) {
  if (!button || typeof action !== 'function') return Promise.resolve();
  const active = pendingButtonActions.get(button);
  if (active) return active;
  if (button.disabled) return Promise.resolve();

  const originalLabel = button.textContent;
  const wasDisabled = button.disabled;
  const hadBusy = button.hasAttribute('aria-busy');
  const originalBusy = button.getAttribute('aria-busy');
  const pendingLabel = String(options.pendingLabel || originalLabel);
  const task = (async () => {
    button.disabled = true;
    button.textContent = pendingLabel;
    button.setAttribute('aria-busy', 'true');
    try {
      return await action();
    } finally {
      button.disabled = wasDisabled;
      button.textContent = originalLabel;
      if (hadBusy) button.setAttribute('aria-busy', originalBusy);
      else button.removeAttribute('aria-busy');
      pendingButtonActions.delete(button);
    }
  })();
  pendingButtonActions.set(button, task);
  return task;
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
  function setScopeStatus(scope, text) {
    const statusId = scope === 'youtube'
      ? 'youtubeSaveStatus'
      : scope === 'advanced'
        ? 'advancedSaveStatus'
        : 'generalSaveStatus';
    setText(statusId, text);
    const status = byId(statusId);
    if (status) {
      const value = String(text || '');
      status.className = 'field-message ' + (value.startsWith('Saved.') ? 'good' : value.startsWith('Save failed:') ? 'bad' : 'muted');
    }
    if (scope === 'advanced') setText('settingsSaveStatus', text);
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
      auto_vod_enabled: val('autoVodEnabled', false),
      auto_youtube_enabled: val('autoYoutubeEnabled', false),
      auto_youtube_cleanup_delay_hours: Number(val('autoYoutubeCleanupDelayHours', '0')),
      auto_vod_poll_minutes: val('autoVodPollMinutes', '60'),
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
    return withButtonPending(btn, {pendingLabel:'Saving...'}, async () => {
      try {
        setScopeStatus(scope, 'Saving...');
        const saved = await postJson('/api/settings', collectSettingsFallback());
        if (saved._settings_file) setText('settingsFilePath', saved._settings_file);
        setScopeStatus(scope, 'Saved.');
      if (typeof window.loadState === 'function') {
        try { await window.loadState(); } catch(e) {}
      }
      if (typeof window.refreshDashboard === 'function') {
        try { await window.refreshDashboard(); } catch(e) {}
      }
      if (typeof window.refreshAutoRecorderStatus === 'function') {
        try { await window.refreshAutoRecorderStatus(); } catch(e) {}
      }
      const label = scope === 'youtube' ? 'YouTube settings' : scope === 'advanced' ? 'Advanced settings' : 'General settings';
      alert(label + ' saved.\n\nFile: ' + (saved._settings_file || 'unknown'));
        return saved;
      } catch (e) {
        console.error(e);
        setScopeStatus(scope, 'Save failed: ' + (e.message || 'Unable to save settings.'));
        alert('Save failed:\n\n' + e.message);
        throw e;
      }
    });
  };

  window.vodCheckSettingsStatus = async function(ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const button = ev?.currentTarget || byId('checkSettingsStatus');
    return withButtonPending(button, {pendingLabel:'Checking...'}, async () => {
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
    });
  };

  document.addEventListener('click', function(ev) {
    const btn = ev.target.closest && ev.target.closest('#saveSettings, #saveYoutubeSettings, #saveAdvancedSettings');
    if (btn) {
      window.vodRobustSaveSettings(ev, btn.id === 'saveYoutubeSettings' ? 'youtube' : btn.id === 'saveAdvancedSettings' ? 'advanced' : 'general');
    }
  }, true);
})();


(function bootstrapNavigationEarly() {
  function byId(id) { return document.getElementById(id); }
  const YTDLP_DEFAULT_OUTPUT_TEMPLATE = '%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s';
const MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = '{date_de} - {streamer} - {title}';

const pageMetaEarly = {
    dashboard: ['Dashboard', 'What needs attention and what is happening now.'],
    search: ['VODs', 'Find, download, and manage Twitch VODs.'],
    live: ['Live', 'Monitor live streamers and recordings.'],
    queue: ['Queue', 'Follow individual VODs from download through YouTube upload.'],
    settings: ['Settings', 'Manage downloads, streamers, YouTube, and advanced options.']
  };
  window.vodShowPage = function(name) {
    if (!pageMetaEarly[name]) name = 'dashboard';
    document.querySelectorAll('.page').forEach(function(page) {
      page.classList.toggle('active', page.id === 'page-' + name);
    });
    document.querySelectorAll('.nav-btn').forEach(function(btn) {
      const active = btn.dataset.page === name;
      btn.classList.toggle('active', active);
      if (active) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
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
let stateLoadPromise = null;
let streamerProfilesByLogin = new Map();
let streamerProfilesLoadPromise = null;
let streamerProfilesLoaded = false;
let searchStreamerLoadState = 'loading';
let searchStreamerLoadError = '';
let searchStreamerSelection = null;
let lastResults = [];
let jobOpenState = {};
let queueDetailOpenState = {};
const pendingAutoYoutubeReleases = new Set();
const pendingAutoYoutubePlaylistActions = new Set();
const pendingAutoYoutubeRecoveries = new Set();
let autoYoutubePlaylistHistoryAutoOpened = false;
let autoExpandJobDetails = localStorage.getItem('vodJobAutoExpand') === '1';
let youtubePlaylistChoices = [];
let streamerProfileDraft = {};
let expandedStreamerLogin = '';
let streamerPolicyEditorDirty = false;
let streamerListDirty = false;
let streamerPolicyFeedback = new Map();
let streamerPolicyEditorDraft = new Map();
let streamerListSearchQuery = '';
let streamerListFilter = 'all';
let liveStreamers = [];
let liveStreamStatuses = new Map();
let liveStatusRequests = new Map();
let liveStatusRefreshPromise = null;
let liveStatusInitialRefreshStarted = false;
let liveOfflineExpanded = false;
let liveStatusUnavailableExpanded = false;
let liveStatusLastUpdatedAt = null;
let liveRecordingJobs = [];
let liveRecordingActions = new Map();
let autoRecorderStatusSnapshot = null;
let autoVodStatusSnapshot = null;

const LIVE_STATUS_CONCURRENCY = 2;
const AUTO_RECORDER_STATUS_REFRESH_MS = 15000;
const ACTIVE_RECORDING_STATES = new Set(['queued', 'running', 'stopping']);

const $ = (id) => document.getElementById(id);

const YTDLP_DEFAULT_OUTPUT_TEMPLATE = '%(uploader)s/%(upload_date)s - %(uploader)s - %(title)s [%(id)s].%(ext)s';
const MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE = '{date_de} - {streamer} - {title}';

const pageMeta = {
  dashboard: ['Dashboard', 'What needs attention and what is happening now.'],
  search: ['VODs', 'Find, download, and manage Twitch VODs.'],
  live: ['Live', 'Monitor live streamers and recordings.'],
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
  if (name === 'search') ensureSearchStreamerPickerStreamers();
  refreshDashboard().catch(() => {});
  if (name === 'search' && !$('localVodsPanel')?.hidden && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});
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

function normalizeStreamerProfileMap(payload) {
  const profiles = payload?.profiles;
  const normalized = new Map();
  if (!profiles || typeof profiles !== 'object') return normalized;
  Object.entries(profiles).forEach(([rawLogin, rawProfile]) => {
    const login = canonicalStreamerLoginClient(rawProfile?.login || rawLogin);
    if (!login || !rawProfile || typeof rawProfile !== 'object' || normalized.has(login)) return;
    normalized.set(login, {
      login,
      display_name: String(rawProfile.display_name || '').trim(),
      avatar_url: String(rawProfile.avatar_url || '').trim()
    });
  });
  return normalized;
}

function loadStreamerProfiles() {
  if (streamerProfilesLoaded) return Promise.resolve(streamerProfilesByLogin);
  if (streamerProfilesLoadPromise) return streamerProfilesLoadPromise;
  streamerProfilesLoadPromise = api('/api/streamer-profiles')
    .then(payload => {
      streamerProfilesByLogin = normalizeStreamerProfileMap(payload);
      return streamerProfilesByLogin;
    })
    .catch(() => {
      streamerProfilesByLogin = new Map();
      return streamerProfilesByLogin;
    })
    .finally(() => {
      streamerProfilesLoaded = true;
      streamerProfilesLoadPromise = null;
    });
  return streamerProfilesLoadPromise;
}

function localCalendarDate(date) {
  const value = new Date(date);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function dateRangeForPreset(preset, now = new Date()) {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  if (preset === 'yesterday-today') start.setDate(start.getDate() - 1);
  else if (preset === 'last-7') start.setDate(start.getDate() - 6);
  else if (preset === 'last-30') start.setDate(start.getDate() - 29);
  return { from: localCalendarDate(start), to: localCalendarDate(end) };
}

function setDatePreset(preset) {
  if (preset !== 'custom') {
    const range = dateRangeForPreset(preset);
    $('fromDate').value = range.from;
    $('toDate').value = range.to;
  }
  document.querySelectorAll('[data-date-preset]').forEach(button => {
    const active = button.dataset.datePreset === preset;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const custom = preset === 'custom';
  $('customDateRange')?.classList.toggle('is-custom', custom);
  const summary = $('resolvedDateRange');
  if (summary) {
    summary.hidden = custom;
    if (!custom) summary.textContent = `${$('fromDate').value} → ${$('toDate').value}`;
  }
}

function setDateRange(days) {
  setDatePreset(days === 1 ? 'today' : (days === 7 ? 'last-7' : 'last-30'));
}

function renderState() {
  $('archiveCount').textContent = `Archive: ${state.archive_count} VODs`;
  if ($('settingsFilePath')) $('settingsFilePath').textContent = state.settings_file || state.settings._settings_file || 'unknown';
  streamerProfileDraft = cloneStreamerProfiles(state.settings.streamer_profiles);
  streamerListDirty = false;
  $('streamersText').value = state.streamers.join('\n');
  renderStreamerEditor();
  updateStreamerListSaveState();
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
  $('autoVodEnabled').checked = state.settings.auto_vod_enabled === true;
  $('autoYoutubeEnabled').checked = state.settings.auto_youtube_enabled === true;
  $('autoYoutubeCleanupDelayHours').value = String(state.settings.auto_youtube_cleanup_delay_hours || 0);
  $('autoVodPollMinutes').value = String(state.settings.auto_vod_poll_minutes || 60);
  $('autoVodPollMinutes').disabled = !$('autoVodEnabled').checked;
  updateAutoVodSettingCopy();
  updateAutoRecorderSettingCopy();
  updateAutoYoutubeSettingCopy();
  $('youtubeEnabled').checked = !!state.settings.youtube_enabled;
  $('youtubeAutoUpload').checked = !!state.settings.youtube_auto_upload;
  syncManualDownloadWorkflowMode();
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
  renderAutomationPolicySummaries();
  renderSearchStreamerCheckboxes();
  updateVodFilterCount();
  syncLiveStreamers(state.streamers);
  renderDashboardVodAutomation();
  renderDashboardLiveRecording();
  renderDashboardLiveSummary();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

async function loadState() {
  if (stateLoadPromise) return stateLoadPromise;
  searchStreamerLoadState = 'loading';
  searchStreamerLoadError = '';
  renderSearchStreamerCheckboxes();
  stateLoadPromise = api('/api/state').then(nextState => {
    state = nextState;
    if (!Array.isArray(state.streamers)) throw new Error('Configured streamers could not be read.');
    searchStreamerSelection = null;
    searchStreamerLoadState = 'ready';
    renderState();
    initializeLiveStatuses();
    loadStreamerProfiles().then(() => {
      if (!state) return;
      if (!streamerPolicyEditorDirty) renderStreamerEditor();
      renderLiveStreams();
    });
  }).catch(error => {
    searchStreamerLoadState = 'error';
    searchStreamerLoadError = error.message || 'Unable to load configured streamers.';
    renderSearchStreamerCheckboxes();
    throw error;
  }).finally(() => {
    stateLoadPromise = null;
  });
  return stateLoadPromise;
}
window.loadState = loadState;


function storedSearchStreamerSelection() {
  try {
    const raw = localStorage.getItem('vodSearchStreamerSelection');
    if (raw === null) return null;
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

function saveSearchStreamerSelection(names) {
  try { localStorage.setItem('vodSearchStreamerSelection', JSON.stringify(names || [])); } catch {}
}

function configuredSearchStreamers() {
  return Array.isArray(state?.streamers) ? [...new Set(state.streamers)] : [];
}

function selectedSearchStreamersFromState() {
  const streamers = configuredSearchStreamers();
  const valid = new Set(streamers);
  if (searchStreamerSelection === null) {
    const stored = storedSearchStreamerSelection();
    searchStreamerSelection = new Set(stored === null ? streamers : stored.filter(name => valid.has(name)));
  } else {
    searchStreamerSelection = new Set([...searchStreamerSelection].filter(name => valid.has(name)));
  }
  return streamers.filter(name => searchStreamerSelection.has(name));
}

function saveSearchStreamerSelectionState() {
  const selected = selectedSearchStreamersFromState();
  saveSearchStreamerSelection(selected);
  return selected;
}

function ensureSearchStreamerPickerStreamers() {
  if (Array.isArray(state?.streamers) || stateLoadPromise) return;
  loadState().catch(() => {});
}

function renderSearchStreamerCheckboxes() {
  const box = $('searchStreamerCheckboxes');
  const info = $('searchStreamerToggleInfo');
  if (!box) return;

  if (searchStreamerLoadState === 'loading') {
    box.innerHTML = '<div class="muted">Loading configured streamers…</div>';
    if (info) {
      info.textContent = 'Loading streamers…';
      info.setAttribute('aria-label', 'Loading configured streamers');
    }
    return;
  }
  if (searchStreamerLoadState === 'error') {
    box.innerHTML = `<div class="bad">Unable to load configured streamers. ${escapeHtml(searchStreamerLoadError)}</div>`;
    if (info) {
      info.textContent = 'Unable to load';
      info.setAttribute('aria-label', 'Unable to load configured streamers');
    }
    return;
  }

  const streamers = configuredSearchStreamers();

  const selectedSet = new Set(selectedSearchStreamersFromState());

  if (!streamers.length) {
    box.innerHTML = '<div class="muted">No configured streamers.</div>';
    if (info) {
      info.textContent = '0 selected';
      info.setAttribute('aria-label', '0 of 0 configured streamers selected');
    }
    return;
  }

  const filter = String($('searchStreamerFilter')?.value || '').trim().toLowerCase();
  const visibleStreamers = streamers.filter(s => !filter || s.toLowerCase().includes(filter));
  box.innerHTML = visibleStreamers.map(s => `
    <label class="streamer-picker-option">
      <input type="checkbox" class="search-streamer-check" value="${escapeHtml(s)}" ${selectedSet.has(s) ? 'checked' : ''}>
      <span class="streamer-picker-identity">${streamerAvatarHtml(s, 'picker')}<span>${escapeHtml(s)}</span></span>
    </label>
  `).join('') || '<div class="muted">No streamers match this filter.</div>';

  wireStreamerAvatarFallbacks(box);
  document.querySelectorAll('.search-streamer-check').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) searchStreamerSelection.add(cb.value);
      else searchStreamerSelection.delete(cb.value);
      saveSearchStreamerSelectionState();
      updateSearchStreamerToggleInfo();
    });
  });
  updateSearchStreamerToggleInfo();
}

function selectedSearchStreamersFromCheckboxes() {
  return selectedSearchStreamersFromState();
}

function updateSearchStreamerToggleInfo() {
  const info = $('searchStreamerToggleInfo');
  if (!info) return;
  if (searchStreamerLoadState !== 'ready') return;
  const selected = selectedSearchStreamersFromCheckboxes();
  const total = configuredSearchStreamers().length;
  info.textContent = `${selected.length} selected`;
  info.setAttribute('aria-label', `${selected.length} of ${total} streamers selected`);
}

function setAllSearchStreamers(checked) {
  searchStreamerSelection = new Set(checked ? configuredSearchStreamers() : []);
  saveSearchStreamerSelectionState();
  renderSearchStreamerCheckboxes();
  updateSearchStreamerToggleInfo();
}

function selectedSearchStreamersForSearch() {
  return selectedSearchStreamersFromCheckboxes();
}

function closeSearchStreamerPicker({ returnFocus = false } = {}) {
  const panel = $('searchStreamerPickerPanel');
  const toggle = $('searchStreamerPickerToggle');
  if (!panel || !toggle) return;
  panel.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
  if (returnFocus) toggle.focus();
}

function toggleSearchStreamerPicker() {
  const panel = $('searchStreamerPickerPanel');
  const toggle = $('searchStreamerPickerToggle');
  if (!panel || !toggle) return;
  const opening = panel.hidden;
  panel.hidden = !opening;
  toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if (opening) $('searchStreamerFilter')?.focus();
}

function updateVodFilterCount() {
  const controls = ['includeUnknownDates', 'strictDateFilter', 'excludeLiveStreams', 'onlyRealVodUrls'];
  const defaults = { includeUnknownDates:true, strictDateFilter:false, excludeLiveStreams:true, onlyRealVodUrls:true };
  const count = controls.filter(id => !!$(id) && $(id).checked !== defaults[id]).length;
  const label = $('vodFilterCount');
  if (label) label.textContent = `${count} active`;
}

function showVodWorkspaceTab(name) {
  const find = name !== 'local';
  const findTab = $('findVodsTab');
  const localTab = $('localVodsTab');
  const findPanel = $('findVodsPanel');
  const localPanel = $('localVodsPanel');
  findTab?.classList.toggle('active', find);
  localTab?.classList.toggle('active', !find);
  findTab?.setAttribute('aria-selected', find ? 'true' : 'false');
  localTab?.setAttribute('aria-selected', find ? 'false' : 'true');
  if (findPanel) {
    findPanel.classList.toggle('active', find);
    findPanel.hidden = !find;
    findPanel.setAttribute('aria-hidden', find ? 'false' : 'true');
  }
  if (localPanel) {
    localPanel.classList.toggle('active', !find);
    localPanel.hidden = find;
    localPanel.setAttribute('aria-hidden', find ? 'true' : 'false');
  }
  if (!find && typeof loadLocalVideos === 'function') loadLocalVideos().catch(() => {});
}


function selectedUrls() {
  return [...document.querySelectorAll('.rowcheck:checked')].map(cb => cb.dataset.url);
}

function refreshSelectionState() {
  const count = selectedUrls().length;
  const eligible = document.querySelectorAll('.rowcheck[data-already-downloaded="false"]');
  const btn = $('downloadSelected');
  if (btn) {
    btn.disabled = count === 0;
    btn.textContent = count ? `Download ${count} Selected` : 'Download Selected';
  }

  const bar = $('searchSelectionBar');
  const stickyBtn = $('downloadSelected');
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
  const clear = $('clearResultsSelection');
  if (clear) clear.classList.toggle('hidden', count === 0);
  const selectReady = $('selectNewResults');
  if (selectReady) selectReady.disabled = eligible.length === 0;
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
    if (profile?.auto_youtube_upload === true) normalizedProfile.auto_youtube_upload = true;
    if (login && Object.keys(normalizedProfile).length && !normalized[login]) {
      normalized[login] = normalizedProfile;
    }
  });
  return normalized;
}

function streamerProfileAutoRecordEnabled(profiles, streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  return !!login && profiles?.[login]?.auto_record === true;
}

function streamerProfileFor(streamer) {
  return streamerProfilesByLogin.get(canonicalStreamerLoginClient(streamer)) || null;
}

function streamerAvatarInitials(streamer, profile=streamerProfileFor(streamer)) {
  const source = String(profile?.display_name || streamer || '').trim();
  if (!source) return '?';
  const parts = source.replace(/[_-]+/g, ' ').split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (profile?.display_name && parts.length > 1) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return parts[0][0].toUpperCase();
}

function streamerAvatarHtml(streamer, size='default') {
  const profile = streamerProfileFor(streamer);
  const avatarUrl = String(profile?.avatar_url || '').trim();
  const initials = streamerAvatarInitials(streamer, profile);
  const image = avatarUrl
    ? `<img class="streamer-avatar-image" src="${escapeHtml(avatarUrl)}" alt="" loading="lazy" decoding="async">`
    : '';
  return `<span class="streamer-avatar streamer-avatar-${escapeHtml(size)}" aria-hidden="true"><span class="streamer-avatar-fallback">${escapeHtml(initials)}</span>${image}</span>`;
}

function streamerAvatarForKnownIdentity(streamer, size='default') {
  return canonicalStreamerLoginClient(streamer) ? streamerAvatarHtml(streamer, size) : '';
}

function wireStreamerAvatarFallbacks(root) {
  root?.querySelectorAll?.('.streamer-avatar-image').forEach(image => {
    image.addEventListener('error', () => image.remove(), {once:true});
  });
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

const VOD_HANDLING_LABELS = {
  manual:'Manual',
  auto_download:'Auto Download',
  download_and_youtube:'Download + YouTube',
  needs_review:'Needs Review'
};

const LIVE_RECORDING_LABELS = {
  manual:'Manual',
  automatic:'Automatic'
};

function automationProductView() {
  return state?.automation_product || {
    streamer_policies:{},
    summary:{manual:0, auto_download:0, download_and_youtube:0, needs_review:0}
  };
}

function streamerPolicyView(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  return login ? automationProductView().streamer_policies?.[login] || null : null;
}

function vodHandlingLabel(mode) {
  return VOD_HANDLING_LABELS[mode] || 'Unavailable';
}

function liveRecordingLabel(mode) {
  return LIVE_RECORDING_LABELS[mode] || 'Manual';
}

function playlistDisplayName(playlistId) {
  const id = String(playlistId || '').trim();
  if (!id) return 'No playlist';
  const match = availablePlaylistChoices(id).find(playlist => playlist.id === id);
  return match?.title || `Saved playlist (${id})`;
}

function policySummaryText(summary=automationProductView().summary) {
  const values = summary || {};
  const parts = [
    `${Number(values.manual) || 0} Manual`,
    `${Number(values.auto_download) || 0} Auto Download`,
    `${Number(values.download_and_youtube) || 0} Download + YouTube`
  ];
  const review = Number(values.needs_review) || 0;
  if (review) parts.push(`${review} Needs Review`);
  return parts.join(' · ');
}

function renderAutomationPolicySummaries() {
  const text = policySummaryText();
  if ($('automationPolicySummary')) $('automationPolicySummary').textContent = text;
  if ($('streamerPolicySummary')) $('streamerPolicySummary').textContent = text;
}

function syncManualDownloadWorkflowMode() {
  const select = $('manualDownloadWorkflowMode');
  if (!select) return;
  const workflow = automationProductView().manual_download_workflow || {};
  select.value = workflow.status === 'enabled'
    ? 'upload_after_download'
    : workflow.status === 'blocked_by_legacy_youtube_gate'
      ? 'needs_review'
      : 'ready_for_review';
  const help = $('manualDownloadWorkflowHelp');
  if (help) {
    help.textContent = select.value === 'needs_review'
      ? 'Automatic upload was requested in the existing configuration, but its legacy YouTube gate is off. Choose a valid handling option to resolve it.'
      : 'Automatic upload requires a connected YouTube account. Explicit uploads from Local VODs remain available independently.';
    help.className = `field-message ${select.value === 'needs_review' ? 'warn' : 'muted'}`;
  }
}

function applyManualDownloadWorkflowChoice() {
  const mode = $('manualDownloadWorkflowMode')?.value;
  if (!mode || mode === 'needs_review') return;
  const upload = mode === 'upload_after_download';
  $('youtubeEnabled').checked = upload;
  $('youtubeAutoUpload').checked = upload;
  const help = $('manualDownloadWorkflowHelp');
  if (help) {
    help.textContent = upload
      ? 'After a manually started download, the legacy follow-up will attempt a YouTube upload when the account is connected.'
      : 'Manually downloaded VODs will remain ready for review. You can still upload them explicitly from Local VODs.';
    help.className = 'field-message muted';
  }
}

function updateStreamerListSaveState() {
  const button = $('saveStreamers');
  const status = $('streamerListSaveStatus');
  if (button) button.disabled = !streamerListDirty;
  if (status) {
    status.textContent = streamerListDirty ? 'Unsaved changes.' : 'No unsaved changes.';
    status.className = 'field-message muted';
  }
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
      ? 'Will run when Automation settings are saved.'
      : 'Will pause when Automation settings are saved. Active recordings will continue.';
    return;
  }
  copy.textContent = persisted
    ? 'Running · only streamers configured for Automatic Live Recording are monitored.'
    : 'Paused · streamer Live Recording policies are preserved.';
}

function formatAutoRecorderTimestamp(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function updateAutoVodSettingCopy() {
  const toggle = $('autoVodEnabled'), interval = $('autoVodPollMinutes'), copy = $('autoVodSettingState');
  if (!toggle || !interval || !copy) return;
  interval.disabled = !toggle.checked;
  const persisted = state?.settings?.auto_vod_enabled === true;
  if (toggle.checked !== persisted) {
    copy.textContent = toggle.checked
      ? 'Will run when Automation settings are saved.'
      : 'Will pause when Automation settings are saved.';
    return;
  }
  copy.textContent = persisted
    ? 'Running · streamer VOD Handling policies are unchanged.'
    : 'Paused · streamer VOD Handling policies are preserved.';
}

function updateAutoYoutubeSettingCopy() {
  const toggle = $('autoYoutubeEnabled');
  const copy = $('autoYoutubeSettingState');
  if (!toggle || !copy) return;
  const persisted = state?.settings?.auto_youtube_enabled === true;
  if (toggle.checked !== persisted) {
    copy.textContent = toggle.checked
      ? 'Will run when Automation settings are saved.'
      : 'Will pause when Automation settings are saved.';
    return;
  }
  copy.textContent = persisted
    ? 'Running · eligible automatic downloads may continue to YouTube.'
    : 'Paused · Download + YouTube streamer policies are preserved.';
}

function autoVodStatusPresentation(snapshot) {
  // Presentation priority: migration, storage, state/discovery errors,
  // baseline confirmation, selection state, then ordinary monitor state.
  if (!snapshot || snapshot.initialized !== true) return {kind:'unavailable', title:'Auto VOD · Unavailable', detail:'Available in the production runtime.'};
  if (!snapshot.enabled) return {kind:'paused', title:'Auto VOD · Off', detail:'Streamer selections are preserved.'};
  const result = snapshot.last_result || {};
  const action = String(result.action || '');
  const storage = String(result.storage_state || 'not_checked');
  const blocked = Math.max(0, Number(result.storage_blocked_count) || 0);
  const singular = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  if (action === 'migration_required') return {kind:'degraded', title:'Auto VOD · Needs attention', detail:'A one-time Auto VOD baseline migration is required before automatic downloads can resume.'};
  if (action === 'storage_unavailable' || storage === 'unavailable') return {kind:'degraded', title:'Auto VOD · Needs attention', detail:'Automatic downloads are paused because storage could not be checked.'};
  if (action === 'storage_insufficient' || storage === 'insufficient' || blocked > 0) {
    const free = Number(result.storage_free_gb), required = Number(result.storage_required_gb);
    const detail = Number.isFinite(free) && Number.isFinite(required)
      ? `Paused: only ${free.toFixed(1)} GB free; ${required.toFixed(1)} GB required.`
      : 'Waiting for storage. Automatic downloads are paused.';
    return {kind:'degraded', title:'Auto VOD · Needs attention', detail};
  }
  if (action === 'state_unhealthy') return {kind:'degraded', title:'Auto VOD · Needs attention', detail:'Automatic VOD scheduling is paused to prevent duplicates.'};
  const errors = Math.max(0, Number(result.error_count) || 0);
  if (errors) return {kind:'degraded', title:'Auto VOD · Needs attention', detail:`${singular(errors, 'streamer')} could not be checked. Auto VOD will try again later.`};
  const baselined = Math.max(0, Number(result.baseline_established_count) || 0);
  if (baselined) return {kind:'running', title:'Auto VOD · Ready', detail:`Baseline saved for ${singular(baselined, 'streamer')}. Existing VODs were not queued.`};
  if (!snapshot.watched_count) return {kind:'paused', title:'Auto VOD · No streamers', detail:'Enable Auto VOD for at least one streamer in Streamers.'};
  if (snapshot.in_progress) return {kind:'running', title:'Auto VOD · Checking…', detail:`Watching ${snapshot.watched_count || 0} streamers`};
  if (!snapshot.last_finished_at) return {kind:'running', title:'Auto VOD · Starting', detail:'Waiting for first check.'};
  const queued = Math.max(0, Number(result.queued_count) || 0);
  const detail = queued ? `Last check: ${singular(queued, 'new VOD')} queued` : `Last checked ${formatAutoRecorderTimestamp(snapshot.last_finished_at)} · Next ${formatAutoRecorderTimestamp(snapshot.next_check_at)}`;
  return {kind:'running', title:'Auto VOD · Running', detail};
}

async function refreshAutoVodStatus() {
  let snapshot; try { snapshot = await api('/api/auto-vod/status'); } catch { snapshot = {unavailable:true}; }
  autoVodStatusSnapshot = snapshot;
  const box = $('autoVodStatus'); if (!box) return snapshot; const view = autoVodStatusPresentation(snapshot);
  box.className = `auto-recorder-status is-${view.kind}`;
  const title = box.querySelector('strong'), detail = box.querySelector('span');
  if (title) title.textContent = view.title;
  if (detail) detail.textContent = view.detail;
  const checkNow = $('checkAutoVodNow');
  if (checkNow) checkNow.disabled = snapshot.initialized !== true || snapshot.running !== true || snapshot.enabled !== true;
  renderDashboardVodAutomation(snapshot);
  return snapshot;
}

function autoRecorderStatusPresentation(snapshot) {
  if (!snapshot) {
    return {kind:'loading', title:'Automatic Live Recording · Checking…', detail:'Loading monitor status.'};
  }
  if (snapshot.unavailable === true) {
    return {kind:'unavailable', title:'Automatic Live Recording status unavailable', detail:'Will retry automatically.'};
  }
  const watched = Math.max(0, Number(snapshot.watched_count) || 0);
  const selectedText = `${watched} streamer${watched === 1 ? '' : 's'} selected`;
  if (snapshot.state_healthy === false || snapshot.phase === 'degraded') {
    const reason = ['invalid_json', 'invalid_structure', 'unsupported_version'].includes(snapshot.last_error_code)
      ? 'State file invalid'
      : ['state_persistence_failed', 'unreadable_state'].includes(snapshot.last_error_code)
        ? 'State file unavailable'
        : 'Monitor error';
    return {kind:'degraded', title:'Automatic Live Recording · Degraded', detail:`${reason} · automatic recordings are paused for safety.`};
  }
  if (snapshot.enabled !== true) {
    return {kind:'paused', title:'Automatic Live Recording · Paused', detail:selectedText};
  }
  if (snapshot.running !== true) {
    return {kind:'unavailable', title:'Automatic Live Recording · Unavailable', detail:'Production monitor is not running.'};
  }
  if (snapshot.phase === 'starting') {
    return {kind:'starting', title:'Automatic Live Recording · Starting', detail:selectedText};
  }
  if (watched === 0) {
    return {kind:'running', title:'Automatic Live Recording · Running', detail:'No streamers selected'};
  }
  const checkedAt = formatAutoRecorderTimestamp(snapshot.last_check_completed_at);
  return {
    kind:'running',
    title:'Automatic Live Recording · Running',
    detail:`Watching ${watched}${checkedAt ? ` · Last checked ${checkedAt}` : ' · Awaiting first check'}`
  };
}

function renderAutoRecorderStatus(snapshot=autoRecorderStatusSnapshot) {
  const box = $('autoRecorderStatus');
  if (!box) return;
  const view = autoRecorderStatusPresentation(snapshot);
  box.className = `auto-recorder-status is-${view.kind}`;
  box.innerHTML = `<strong>${escapeHtml(view.title)}</strong><span>${escapeHtml(view.detail)}</span>`;
  renderDashboardLiveRecording(snapshot);
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
  return !!job || status.state === 'live';
}

function liveStreamerHasUnavailableStatus(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  const status = liveStreamStatuses.get(login) || {state:'unknown'};
  return status.state === 'error' && !recordingJobForStreamer(login);
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

function liveStreamSummaryText(featured, offline, unavailable=[]) {
  const liveCount = featured.filter(streamer => {
    const login = canonicalStreamerLoginClient(streamer);
    return liveStreamStatuses.get(login)?.state === 'live';
  }).length;
  const recordingCount = featured.filter(streamer => {
    const job = recordingJobForStreamer(streamer);
    return !!job && ACTIVE_RECORDING_STATES.has(job.state);
  }).length;
  const summary = `${liveCount} Live · ${recordingCount} Recording · ${offline.length} Offline`;
  return unavailable.length ? `${summary} · ${unavailable.length} Unavailable` : summary;
}

function renderDashboardLiveSummary() {
  const summary = $('dashboardLiveSummary');
  const names = $('dashboardLiveNames');
  if (!summary || !names) return;
  const section = summary.closest?.('.dashboard-live-summary');
  if (!liveStreamers.length) {
    section?.classList.add('is-empty');
    summary.textContent = 'No streamers configured.';
    names.innerHTML = '';
    return;
  }
  const live = liveStreamers.filter(streamer => {
    const login = canonicalStreamerLoginClient(streamer);
    return liveStreamStatuses.get(login)?.state === 'live';
  });
  const recordings = liveRecordingJobs.filter(job => ACTIVE_RECORDING_STATES.has(job?.state)).length;
  section?.classList.toggle('is-empty', live.length === 0 && recordings === 0);
  summary.textContent = `${live.length} Live · ${recordings} Recording`;
  names.innerHTML = live.slice(0, 3).map(streamer => `<span><i aria-hidden="true"></i>${escapeHtml(streamer)}</span>`).join('') + (live.length > 3 ? `<span>+${live.length - 3} more</span>` : '');
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
  const titleHtml = title ? `<span class="live-stream-title">${escapeHtml(title)}</span>` : '';
  const metadata = [
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
      <div class="live-stream-primary"><span class="live-stream-state">${escapeHtml(statusLabel)}</span><span class="live-stream-identity">${streamerAvatarHtml(streamer, 'live')}<strong class="live-stream-name">${escapeHtml(streamer)}</strong></span></div>
      <div class="live-stream-details">${titleHtml}<div class="live-stream-footer"><div class="live-stream-metadata">${metadata}</div><div class="live-stream-actions">${actionHtml}</div></div></div>
    </div>
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
    ${streamerAvatarHtml(streamer, 'small')}
    <strong>${escapeHtml(streamer)}</strong>
    <span>${escapeHtml(refreshLabel)}</span>
  </div>`;
}

function renderUnavailableStreamer(streamer) {
  const login = canonicalStreamerLoginClient(streamer);
  return `<div class="offline-stream-item status-unavailable-item" data-live-streamer="${escapeHtml(login)}">
    <span class="offline-stream-indicator" aria-hidden="true"></span>
    ${streamerAvatarHtml(streamer, 'small')}
    <strong>${escapeHtml(streamer)}</strong>
    <span>Status could not be loaded</span>
  </div>`;
}

function toggleOfflineStreamers() {
  liveOfflineExpanded = !liveOfflineExpanded;
  renderLiveStreams();
}

function toggleUnavailableLiveStreamers() {
  liveStatusUnavailableExpanded = !liveStatusUnavailableExpanded;
  renderLiveStreams();
}

function renderLiveStreams() {
  const box = $('liveStreamsList');
  if (!box) return;
  const summary = $('liveStreamsSummary');
  const dashboardSummary = $('dashboardLiveSummary');
  const activeBox = $('liveActiveRecordings');
  const activeSection = activeBox?.closest('.active-recordings-section');
  if (!liveStreamers.length) {
    if (summary) summary.textContent = '0 Live · 0 Recording · 0 Offline';
    renderDashboardLiveSummary();
    if (activeBox) activeBox.innerHTML = '<div class="live-stream-empty muted">No active recordings.</div>';
    activeSection?.classList.add('is-empty');
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
  const unavailable = indexed
    .filter(item => liveStreamerHasUnavailableStatus(item.streamer))
    .map(item => item.streamer);
  const summaryText = liveStreamSummaryText(featured, offline, unavailable);
  if (summary) summary.textContent = summaryText;
  if (dashboardSummary) dashboardSummary.textContent = liveStreamSummaryText(featured, offline).replace(/ · \d+ Offline$/, '');
  renderDashboardLiveSummary();
  const initialCheckPending = liveStatusLastUpdatedAt === null && (
    liveStatusRefreshPromise !== null
    || liveStatusRequests.size > 0
    || indexed.some(item => {
      const login = canonicalStreamerLoginClient(item.streamer);
      const state = liveStreamStatuses.get(login)?.state || 'unknown';
      return state === 'unknown' || state === 'checking';
    })
  );
  const activeRecordings = featured.filter(streamer => {
    const job = recordingJobForStreamer(streamer);
    return !!job && ACTIVE_RECORDING_STATES.has(job.state);
  });
  const liveNow = activeBox
    ? featured.filter(streamer => !activeRecordings.includes(streamer))
    : featured;
  if (activeBox) {
    activeSection?.classList.toggle('is-empty', !activeRecordings.length);
    activeBox.innerHTML = activeRecordings.length
      ? `<div class="live-stream-grid live-active-recording-grid${activeRecordings.length === 1 ? ' is-single' : ''}">${activeRecordings.map(streamer => renderLiveStreamCard(streamer)).join('')}</div>`
      : '<div class="live-stream-empty muted">No active recordings.</div>';
  }
  const liveContent = liveNow.length
    ? `<div class="live-stream-grid${liveNow.length === 1 ? ' is-single' : ''}">${liveNow.map(streamer => renderLiveStreamCard(streamer)).join('')}</div>`
    : `<div class="live-stream-empty muted">${initialCheckPending ? 'Checking live status…' : 'No configured streamer is currently live.'}</div>`;
  const offlineContent = offline.length
    ? `<section class="offline-streams">
        <button type="button" class="offline-streams-toggle" aria-expanded="${liveOfflineExpanded ? 'true' : 'false'}" aria-controls="offlineStreamersList" aria-label="${liveOfflineExpanded ? 'Hide' : 'Show'} ${offline.length} offline streamers">
          <span>Offline Streamers · ${offline.length}</span><span>${liveOfflineExpanded ? 'Hide' : 'Show'}</span>
        </button>
        <div id="offlineStreamersList" class="offline-stream-grid"${liveOfflineExpanded ? '' : ' hidden'}>${offline.map(streamer => renderOfflineStreamer(streamer)).join('')}</div>
      </section>`
    : '';
  const unavailableContent = unavailable.length
    ? `<section class="offline-streams live-status-unavailable">
        <button type="button" class="offline-streams-toggle status-unavailable-toggle" aria-expanded="${liveStatusUnavailableExpanded ? 'true' : 'false'}" aria-controls="unavailableLiveStreamersList" aria-label="${liveStatusUnavailableExpanded ? 'Hide' : 'Show'} ${unavailable.length} streamers with unavailable live status">
          <span>Status unavailable · ${unavailable.length}</span><span>${liveStatusUnavailableExpanded ? 'Hide' : 'Show'}</span>
        </button>
        <div id="unavailableLiveStreamersList" class="offline-stream-grid"${liveStatusUnavailableExpanded ? '' : ' hidden'}>${unavailable.map(streamer => renderUnavailableStreamer(streamer)).join('')}</div>
      </section>`
    : '';
  box.innerHTML = liveContent + unavailableContent + offlineContent;
  const actionRoots = [box, activeBox].filter(Boolean);
  actionRoots.forEach(root => root.querySelectorAll('.live-recording-start').forEach(button => button.addEventListener('click', () => {
    startLiveRecording(button.dataset.streamer).catch(() => {});
  })));
  actionRoots.forEach(root => root.querySelectorAll('.live-recording-stop').forEach(button => button.addEventListener('click', () => {
    stopLiveRecording(button.dataset.jobId, button.dataset.streamer).catch(() => {});
  })));
  actionRoots.forEach(wireStreamerAvatarFallbacks);
  box.querySelector('.status-unavailable-toggle')?.addEventListener('click', toggleUnavailableLiveStreamers);
  box.querySelector('.offline-streams:not(.live-status-unavailable) .offline-streams-toggle')?.addEventListener('click', toggleOfflineStreamers);
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
  const message = $('liveStreamsRefreshStatus');
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

const STREAMER_LIST_FILTERS = new Set([
  'all', 'automated', 'live-recording', 'needs-review'
]);

function normalizeStreamerListSearch(value) {
  return String(value || '').trim().toLocaleLowerCase();
}

function streamerWorkspaceEntries(names, product, searchQuery='', filter='all') {
  const query = normalizeStreamerListSearch(searchQuery);
  const selectedFilter = STREAMER_LIST_FILTERS.has(filter) ? filter : 'all';
  const policies = product?.streamer_policies || {};
  return (names || []).map((name, index) => {
    const login = canonicalStreamerLoginClient(name);
    return {name, index, login, policy: login ? policies[login] || null : null};
  }).filter(entry => {
    if (query && !String(entry.name || '').toLocaleLowerCase().includes(query)) return false;
    const policy = entry.policy;
    if (selectedFilter === 'automated') return ['auto_download', 'download_and_youtube'].includes(policy?.vod_handling);
    if (selectedFilter === 'live-recording') return policy?.live_recording === 'automatic';
    if (selectedFilter === 'needs-review') return policy?.validation?.state === 'needs_review';
    return true;
  });
}

function captureExpandedStreamerPolicyDraft() {
  if (!expandedStreamerLogin) return;
  const row = document.querySelector(`[data-streamer-login="${expandedStreamerLogin}"]`);
  const vodHandling = row?.querySelector('.streamer-vod-handling-select')?.value;
  const liveRecording = row?.querySelector('.streamer-live-recording-select')?.value;
  const playlistId = row?.querySelector('.streamer-playlist-select')?.value;
  if (vodHandling !== undefined && liveRecording !== undefined && playlistId !== undefined) {
    streamerPolicyEditorDraft.set(expandedStreamerLogin, {
      vod_handling: vodHandling,
      live_recording: liveRecording,
      youtube_playlist_id: playlistId
    });
  }
}

function setStreamerListDiscovery({searchQuery=streamerListSearchQuery, filter=streamerListFilter}={}) {
  captureExpandedStreamerPolicyDraft();
  streamerListSearchQuery = String(searchQuery || '');
  streamerListFilter = STREAMER_LIST_FILTERS.has(filter) ? filter : 'all';
  if ($('streamerListSearch')) $('streamerListSearch').value = streamerListSearchQuery;
  document.querySelectorAll('[data-streamer-filter]').forEach(button => {
    const active = button.dataset.streamerFilter === streamerListFilter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  renderStreamerEditor();
}

function streamerEditorValues(login, policy) {
  const draft = streamerPolicyEditorDraft.get(login) || {};
  return {
    vod_handling: draft.vod_handling ?? (policy?.validation?.state === 'needs_review' ? '' : policy?.vod_handling || 'manual'),
    live_recording: draft.live_recording ?? (policy?.live_recording || 'manual'),
    youtube_playlist_id: draft.youtube_playlist_id ?? (policy?.youtube_playlist_id || '')
  };
}

function setStreamerEditorNames(names, {dirty=true}={}) {
  $('streamersText').value = (names || []).join('\n');
  if (dirty) streamerListDirty = true;
  renderStreamerEditor();
  updateStreamerListSaveState();
}

function renderStreamerEditor() {
  const list = $('streamerEditorList');
  if (!list || !$('streamersText')) return;
  const names = streamerEditorNames();
  if (expandedStreamerLogin && !names.some(name => canonicalStreamerLoginClient(name) === expandedStreamerLogin)) {
    expandedStreamerLogin = '';
    streamerPolicyEditorDirty = false;
  }
  if (!names.length) {
    if ($('streamerFilterSummary')) $('streamerFilterSummary').textContent = '0 streamers';
    list.innerHTML = '<div class="streamer-editor-empty muted">No streamers configured yet. Add one to begin with Manual handling.</div>';
    return;
  }
  const visibleEntries = streamerWorkspaceEntries(
    names, automationProductView(), streamerListSearchQuery, streamerListFilter
  );
  const discoveryActive = !!normalizeStreamerListSearch(streamerListSearchQuery) || streamerListFilter !== 'all';
  if ($('streamerFilterSummary')) {
    $('streamerFilterSummary').textContent = discoveryActive
      ? `${names.length} streamer${names.length === 1 ? '' : 's'} · ${visibleEntries.length} shown`
      : `${names.length} streamer${names.length === 1 ? '' : 's'}`;
  }
  if (!visibleEntries.length) {
    list.innerHTML = '<div class="streamer-editor-empty muted">No streamers match these filters.</div>';
    return;
  }
  list.innerHTML = visibleEntries.map(({name, index, login, policy}) => {
    const expanded = !!login && login === expandedStreamerLogin;
    const mode = policy?.vod_handling || 'manual';
    const needsReview = policy?.validation?.state === 'needs_review';
    const editorValues = streamerEditorValues(login, policy);
    const editorMode = editorValues.vod_handling;
    const playlistId = policy?.youtube_playlist_id || '';
    const validationLabel = policy ? (needsReview ? 'Needs Review' : '') : 'Not saved';
    const validationBadge = validationLabel ? `<span class="streamer-validation is-${needsReview ? 'review' : 'pending'}">${validationLabel}</span>` : '';
    const feedback = streamerPolicyFeedback.get(login) || '';
    const editorId = `streamerPolicyEditor-${login || index}`;
    const reviewId = `streamerPolicyReview-${login || index}`;
    const modeOptions = needsReview && !editorMode
      ? '<option value="" selected disabled>Choose a valid VOD Handling mode</option>'
      : '';
    const editor = expanded && policy ? `<div class="streamer-policy-editor" id="${editorId}">
      ${needsReview ? `<div class="streamer-policy-warning" id="${reviewId}" role="alert"><strong>Configuration needs review</strong><span>YouTube automation is enabled for this streamer, but automatic VOD download is not. Choose a valid VOD Handling mode to resolve this configuration.</span></div>` : ''}
      <div class="streamer-policy-fields">
        <label>VOD Handling<select class="streamer-vod-handling-select" aria-describedby="${needsReview ? reviewId : ''}">${modeOptions}<option value="manual" ${editorMode === 'manual' ? 'selected' : ''}>Manual</option><option value="auto_download" ${editorMode === 'auto_download' ? 'selected' : ''}>Auto Download</option><option value="download_and_youtube" ${editorMode === 'download_and_youtube' ? 'selected' : ''}>Download + YouTube</option></select></label>
        <label>YouTube Playlist<select class="streamer-playlist-select">${playlistOptionsHtml(editorValues.youtube_playlist_id, 'No playlist')}</select><span class="field-help">Optional. Blank means no playlist for automatic uploads.</span></label>
        <label>Live Recording<select class="streamer-live-recording-select"><option value="manual" ${editorValues.live_recording === 'manual' ? 'selected' : ''}>Manual</option><option value="automatic" ${editorValues.live_recording === 'automatic' ? 'selected' : ''}>Automatic</option></select></label>
      </div>
      ${editorMode === 'download_and_youtube' && state?.settings?.auto_youtube_enabled !== true ? '<p class="streamer-global-pause-note">Configured for Download + YouTube · Automatic YouTube Processing is currently paused globally.</p>' : ''}
      <div class="streamer-policy-editor-footer"><p class="streamer-policy-feedback muted" role="status" aria-live="polite">${escapeHtml(feedback || 'No unsaved changes.')}</p><div class="button-row"><button type="button" class="quiet-button streamer-policy-cancel">Cancel</button><button type="button" class="primary streamer-policy-save">Save changes</button></div></div>
    </div>` : '';
    return `<article class="streamer-editor-row${expanded ? ' is-expanded' : ''}${needsReview ? ' needs-review' : ''}" data-streamer-index="${index}" data-streamer-login="${escapeHtml(login)}"><div class="streamer-row-summary"><span class="streamer-order" aria-label="Position ${index + 1}">${index + 1}</span><div class="streamer-row-identity">${streamerAvatarHtml(name, 'compact')}<strong>${escapeHtml(name)}</strong>${validationBadge}</div><dl class="streamer-policy-summary"><div><dt>VOD Handling</dt><dd>${escapeHtml(policy ? vodHandlingLabel(mode) : 'Manual after save')}</dd></div><div><dt>Playlist</dt><dd title="${escapeHtml(playlistId)}">${escapeHtml(policy ? playlistDisplayName(playlistId) : 'No playlist')}</dd></div><div><dt>Live Recording</dt><dd>${escapeHtml(policy ? liveRecordingLabel(policy.live_recording) : 'Manual after save')}</dd></div></dl><button type="button" class="quiet-button streamer-edit-button" aria-expanded="${expanded ? 'true' : 'false'}" aria-controls="${editorId}" ${policy ? '' : 'disabled'}>${expanded ? 'Close' : 'Edit'}</button><details class="streamer-secondary-actions"><summary aria-label="More actions for ${escapeHtml(name)}">More</summary><div><button type="button" data-streamer-action="up" aria-label="Move ${escapeHtml(name)} up" ${index === 0 ? 'disabled' : ''}>Move up</button><button type="button" data-streamer-action="down" aria-label="Move ${escapeHtml(name)} down" ${index === names.length - 1 ? 'disabled' : ''}>Move down</button><button type="button" class="danger-outline" data-streamer-action="remove" aria-label="Remove ${escapeHtml(name)}">Remove</button></div></details></div>${editor}</article>`;
  }).join('');
  wireStreamerAvatarFallbacks(list);
  list.querySelectorAll('.streamer-edit-button').forEach(button => button.addEventListener('click', () => {
    const login = button.closest('[data-streamer-login]')?.dataset.streamerLogin || '';
    if (streamerPolicyEditorDirty && login !== expandedStreamerLogin) {
      showToast('Save or cancel the current streamer changes first.', 'warn');
      return;
    }
    const closing = expandedStreamerLogin === login;
    if (closing) streamerPolicyEditorDraft.delete(login);
    expandedStreamerLogin = closing ? '' : login;
    streamerPolicyEditorDirty = false;
    streamerPolicyFeedback.delete(login);
    renderStreamerEditor();
    if (expandedStreamerLogin) document.querySelector(`[data-streamer-login="${expandedStreamerLogin}"] .streamer-vod-handling-select`)?.focus();
  }));
  list.querySelectorAll('.streamer-policy-editor select').forEach(select => {
    select.addEventListener('change', () => {
      streamerPolicyEditorDirty = true;
      captureExpandedStreamerPolicyDraft();
      const status = select.closest('.streamer-policy-editor')?.querySelector('.streamer-policy-feedback');
      if (status) status.textContent = 'Unsaved policy changes.';
    });
  });
  list.querySelectorAll('.streamer-policy-cancel').forEach(button => button.addEventListener('click', () => {
    streamerPolicyEditorDirty = false;
    streamerPolicyFeedback.delete(expandedStreamerLogin);
    streamerPolicyEditorDraft.delete(expandedStreamerLogin);
    expandedStreamerLogin = '';
    renderStreamerEditor();
  }));
  list.querySelectorAll('.streamer-policy-save').forEach(button => button.addEventListener('click', () => {
    saveStreamerPolicy(button.closest('[data-streamer-login]'), button).catch(() => {});
  }));
  list.querySelectorAll('[data-streamer-action]').forEach(button => button.addEventListener('click', () => {
    if (streamerPolicyEditorDirty) {
      showToast('Save or cancel the current streamer changes first.', 'warn');
      return;
    }
    const row = button.closest('[data-streamer-index]');
    const index = Number(row.dataset.streamerIndex);
    const current = streamerEditorNames();
    const action = button.dataset.streamerAction;
    if (action === 'remove') current.splice(index, 1);
    if (action === 'up' && index > 0) [current[index - 1], current[index]] = [current[index], current[index - 1]];
    if (action === 'down' && index < current.length - 1) [current[index + 1], current[index]] = [current[index], current[index + 1]];
    setStreamerEditorNames(current, {dirty:true});
  }));
}

async function saveStreamerPolicy(row, button) {
  const login = row?.dataset.streamerLogin || '';
  const name = streamerEditorNames().find(item => canonicalStreamerLoginClient(item) === login);
  const vodHandling = row?.querySelector('.streamer-vod-handling-select')?.value || '';
  const liveRecording = row?.querySelector('.streamer-live-recording-select')?.value || '';
  const playlistId = row?.querySelector('.streamer-playlist-select')?.value || '';
  const feedback = row?.querySelector('.streamer-policy-feedback');
  if (!vodHandling) {
    if (feedback) { feedback.textContent = 'Choose a valid VOD Handling mode before saving.'; feedback.className = 'streamer-policy-feedback bad'; }
    row?.querySelector('.streamer-vod-handling-select')?.focus();
    return;
  }
  const originalText = button?.textContent || 'Save changes';
  const hadBusy = button?.hasAttribute('aria-busy');
  const originalBusy = button?.getAttribute('aria-busy');
  if (button) { button.disabled = true; button.textContent = 'Saving...'; button.setAttribute('aria-busy', 'true'); }
  if (feedback) { feedback.textContent = 'Saving...'; feedback.className = 'streamer-policy-feedback muted'; }
  try {
    const saved = await api('/api/streamers/policy', {
      method:'POST',
      body:JSON.stringify({streamer:name, vod_handling:vodHandling, live_recording:liveRecording, youtube_playlist_id:playlistId})
    });
    state.settings.streamer_profiles = saved.streamer_profiles;
    state.automation_product = saved.automation_product;
    streamerProfileDraft = cloneStreamerProfiles(saved.streamer_profiles);
    streamerPolicyEditorDirty = false;
    streamerPolicyEditorDraft.delete(login);
    streamerPolicyFeedback.set(login, 'Saved.');
    renderStreamerEditor();
    renderAutomationPolicySummaries();
    renderDashboardVodAutomation();
    refreshAutoRecorderStatus().catch(() => {});
    refreshAutoVodStatus().catch(() => {});
  } catch (error) {
    if (feedback) { feedback.textContent = 'Save failed: ' + (error.message || 'Streamer policy could not be saved.'); feedback.className = 'streamer-policy-feedback bad'; }
    throw error;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
      if (hadBusy) button.setAttribute('aria-busy', originalBusy);
      else button.removeAttribute('aria-busy');
    }
  }
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
  setStreamerEditorNames(names, {dirty:true});
  input.value = '';
  input.focus();
}

function ensureActiveSettingsTabVisible(tab) {
  const strip = tab?.closest('.settings-tabs');
  if (!strip || !tab) return;
  const stripBounds = strip.getBoundingClientRect();
  const tabBounds = tab.getBoundingClientRect();
  if (tabBounds.left < stripBounds.left) {
    strip.scrollLeft += tabBounds.left - stripBounds.left;
  } else if (tabBounds.right > stripBounds.right) {
    strip.scrollLeft += tabBounds.right - stripBounds.right;
  }
}

function showSettingsTab(name) {
  const allowed = ['general', 'automation', 'streamers', 'youtube', 'advanced'];
  const target = allowed.includes(name) ? name : 'general';
  let activeTab = null;
  document.querySelectorAll('.settings-tab').forEach(tab => {
    const active = tab.dataset.settingsTab === target;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
    tab.setAttribute('tabindex', active ? '0' : '-1');
    if (active) activeTab = tab;
  });
  ensureActiveSettingsTabVisible(activeTab);
  document.querySelectorAll('.settings-panel').forEach(panel => {
    const active = panel.dataset.settingsPanel === target;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
    panel.setAttribute('aria-hidden', active ? 'false' : 'true');
  });
  try { localStorage.setItem('vodSettingsTab', target); } catch {}
  if (target === 'youtube') {
    refreshYoutubeStatus().catch(() => {});
  }
  if (target === 'youtube' || target === 'streamers') {
    loadYoutubePlaylists().catch(() => {});
  }
  if (target === 'automation' || target === 'streamers') {
    renderAutomationPolicySummaries();
  }
  if (target === 'streamers' && !streamerPolicyEditorDirty) {
    renderStreamerEditor();
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
  const confirmed = await confirmAction({
    title:'Start selected downloads',
    message:`Download ${selected.length} VOD(s)?\n\n${groupLines}\n\nReview the streamer list before starting.`,
    confirmLabel:'Start downloads'
  });
  if (!confirmed) return;
  const data = await startDownload(selected.map(r => r.url), 'Date Range Selection');
  const batchModeLabel = ($('batchPostprocessMode') && $('batchPostprocessMode').value === 'after_all') ? 'Download all, then post-process' : 'Post-process after each VOD';
  showToast(`Download queue started: ${data.url_count || selected.length} VOD(s). Mode: ${batchModeLabel}`, {variant:'success'});
  showPage('queue');
}

function searchResultStatusHtml(result) {
  const outsideRange = result.outside_range ? '<br><span class="warn">Outside Date Range</span>' : '';
  if (result.already_downloaded) return `Already in Archive${outsideRange}`;
  if (result.auto_vod_baseline_existing) return `Baseline<br><span class="muted">Manual download available</span>${outsideRange}`;
  return `Ready to Download${outsideRange}`;
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
  const resultsCard = body?.closest?.('.search-results-card');
  if (!lastResults.length) {
    resultsCard?.classList.add('is-empty');
    body.innerHTML = '<tr><td colspan="6" class="muted">No matching VODs found. Try expanding the date range.</td></tr>';
    refreshSelectionState();
    return;
  }

  resultsCard?.classList.remove('is-empty');
  const groups = new Map();
  lastResults.forEach(r => {
    const key = r.streamer || 'unknown';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  });

  const rows = [];
  for (const [streamer, items] of groups.entries()) {
    const openCount = items.filter(r => !r.already_downloaded).length;
    rows.push(`<tr class="streamer-group-row"><td colspan="6"><div class="streamer-group-head"><div class="streamer-group-identity">${streamerAvatarForKnownIdentity(streamer, 'group')}<div><strong>${escapeHtml(streamer)}</strong><span>${items.length} VOD(s), ${openCount} ready to download</span></div></div><div><button type="button" class="group-select" data-streamer="${escapeHtml(streamer)}">Select All</button><button type="button" class="group-clear" data-streamer="${escapeHtml(streamer)}">Clear</button></div></div></td></tr>`);
    items.forEach(r => {
      rows.push(`
        <tr class="vod-result-row">
          <td class="vod-result-select"><input class="rowcheck" type="checkbox" data-url="${escapeHtml(r.url)}" data-streamer="${escapeHtml(r.streamer)}" data-already-downloaded="${r.already_downloaded ? 'true' : 'false'}"></td>
          <td data-label="Date" class="vod-result-date">${escapeHtml(r.date)}</td>
          <td data-label="Streamer" class="vod-result-streamer">${escapeHtml(r.streamer)}</td>
          <td data-label="Title" class="vod-result-title">${escapeHtml(r.title)}</td>
          <td data-label="Status" class="vod-result-status ${r.already_downloaded ? 'good' : ''}">${searchResultStatusHtml(r)}</td>
          <td data-label="Link" class="vod-result-link"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">Open Twitch</a></td>
        </tr>`);
    });
  }
  body.innerHTML = rows.join('');
  wireStreamerAvatarFallbacks(body);
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
  try {
    const data = await api('/api/search', { method:'POST', body: JSON.stringify({ streamers, from: $('fromDate').value, to: $('toDate').value, limit: $('limit').value, include_unknown_dates: $('includeUnknownDates').checked, strict_date_filter: $('strictDateFilter').checked, exclude_live_streams: $('excludeLiveStreams').checked, only_real_vod_urls: $('onlyRealVodUrls').checked }) });
    renderResults(data.results, data.errors, data.debug);
  } catch (error) {
    lastResults = [];
    $('resultsBody').innerHTML = '<tr><td colspan="6" class="bad">Search failed. Check the filters and try again.</td></tr>';
    $('searchErrors').innerHTML = `<div class="errorbox">${escapeHtml(error.message || 'Search failed.')}</div>`;
    if ($('searchResultSummary')) $('searchResultSummary').textContent = 'Search failed.';
    refreshSelectionState();
    throw error;
  }
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
    auto_vod_enabled:$('autoVodEnabled').checked,
    auto_youtube_enabled:$('autoYoutubeEnabled').checked,
    auto_youtube_cleanup_delay_hours:Number($('autoYoutubeCleanupDelayHours').value || 0),
    auto_vod_poll_minutes:$('autoVodPollMinutes').value,
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

function markAutomationSettingsDirty() {
  const status = $('automationSaveStatus');
  if (status) { status.textContent = 'Unsaved changes.'; status.className = 'field-message muted'; }
}

async function saveAutomationSettings() {
  const button = $('saveAutomationSettings');
  const status = $('automationSaveStatus');
  return withButtonPending(button, {pendingLabel:'Saving...'}, async () => {
    if (status) { status.textContent = 'Saving...'; status.className = 'field-message muted'; }
    try {
      const saved = await api('/api/settings', {
      method:'POST',
      body:JSON.stringify({
        auto_vod_enabled:$('autoVodEnabled').checked,
        auto_vod_poll_minutes:Number($('autoVodPollMinutes').value || 60),
        auto_youtube_enabled:$('autoYoutubeEnabled').checked,
        auto_recorder_enabled:$('autoRecorderEnabled').checked,
        auto_youtube_cleanup_delay_hours:Number($('autoYoutubeCleanupDelayHours').value || 0)
      })
      });
      state.settings = {...state.settings, ...saved};
      updateAutoVodSettingCopy();
      updateAutoYoutubeSettingCopy();
      updateAutoRecorderSettingCopy();
      if (status) { status.textContent = 'Saved. Streamer policies were not changed.'; status.className = 'field-message good'; }
      await Promise.all([
      refreshAutoVodStatus().catch(() => null),
      refreshAutoRecorderStatus().catch(() => null),
      refreshDashboard().catch(() => null)
      ]);
    } catch (error) {
      if (status) { status.textContent = 'Save failed: ' + (error.message || 'Automation settings could not be saved.'); status.className = 'field-message bad'; }
    }
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
  box.hidden = !estimate;
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
        streamer: job.streamer || meta.streamer || '', date: meta.date || '', title: job.display_title || meta.title || (vodId ? `Twitch VOD ${vodId}` : job.label),
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
  if (
    type === 'upload'
    && item.job?.origin === 'auto_youtube'
    && item.job?.execution_deferred === true
    && item.state === 'waiting'
  ) return {
    status:item.job?.auto_youtube_execution_policy === 'automatic'
      ? 'Preparing automatic upload'
      : 'Ready for YouTube',
    support:item.job?.auto_youtube_execution_policy === 'automatic'
      ? 'Automatic release is pending.'
      : 'Waiting for manual start.',
    reviewRequired:false,
  };
  if (
    type === 'upload'
    && item.job?.origin === 'auto_youtube'
    && item.job?.auto_youtube_execution_policy === 'automatic'
    && item.job?.execution_deferred === false
    && item.state === 'waiting'
  ) return {
    status:'Upload queued automatically',
    support:'Waiting for the upload queue.',
    reviewRequired:false,
  };
  if (
    type === 'upload'
    && item.job?.origin === 'auto_youtube'
    && item.state === 'completed'
    && item.job?.auto_youtube_playlist?.state === 'playlist_pending'
  ) return {
    status:'Playlist pending',
    support:'Video uploaded. Add it to the frozen YouTube playlist when ready.',
    reviewRequired:false,
  };
  if (
    type === 'upload'
    && item.job?.origin === 'auto_youtube'
    && item.state === 'completed'
    && item.job?.auto_youtube_playlist?.state === 'needs_attention'
  ) return {
    status:'Playlist needs attention',
    support:'Video uploaded, but playlist membership could not be confirmed safely.',
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
  if (item.index === 0 && item.job?.auto_youtube_cleanup?.reason) {
    values.push(['Local cleanup reason', item.job.auto_youtube_cleanup.reason]);
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
  const bundleStates = Array.isArray(item.job?.item_states) ? item.job.item_states : [];
  const bundleFailureKinds = Array.isArray(item.job?.item_failure_kinds) ? item.job.item_failure_kinds : [];
  const canStartAutoYoutube = item.index === 0
    && item.job?.type === 'youtube_upload'
    && item.job?.origin === 'auto_youtube'
    && item.job?.auto_youtube_execution_policy !== 'automatic'
    && item.job?.execution_deferred === true
    && item.job?.state === 'queued'
    && bundleStates.length > 0
    && bundleStates.every(state => state === 'queued')
    && !bundleFailureKinds.some(kind => kind === 'uncertain');
  const playlistStatus = item.job?.auto_youtube_playlist || {};
  const cleanupStatus = item.job?.auto_youtube_cleanup || {};
  const cleanupLabels = {
    scheduled: 'Local cleanup scheduled',
    due: 'Local cleanup due',
    keep_local: 'Local copy kept',
    cleaning: 'Removing local copy',
    removed: 'Local copy removed',
    needs_attention: 'Local cleanup needs attention',
    local_copy_missing: 'Local copy unavailable',
  };
  const cleanupNote = item.index === 0 && cleanupLabels[cleanupStatus.state]
    ? `<div class="queue-item-note auto-youtube-cleanup-status">${escapeHtml(cleanupLabels[cleanupStatus.state])}</div>`
    : '';
  const canAddAutoYoutubePlaylist = item.index === 0
    && item.job?.type === 'youtube_upload'
    && item.job?.origin === 'auto_youtube'
    && item.state === 'completed'
    && item.job?.state === 'completed'
    && playlistStatus.eligible === true;
  const recoveryStatus = item.job?.auto_youtube_recovery || {};
  const canRecoverUncertainAutoYoutube = item.job?.type === 'youtube_upload'
    && item.job?.origin === 'auto_youtube'
    && ['error', 'interrupted'].includes(item.state)
    && Array.isArray(recoveryStatus.eligible_item_ids)
    && recoveryStatus.eligible_item_ids.includes(itemId);
  if (canStartAutoYoutube) actionButtons.push(`<button type="button" class="primary queue-item-action" data-queue-action="start-auto-youtube" data-job-id="${escapeHtml(item.job.id)}" data-part-count="${bundleStates.length}" aria-label="Start YouTube upload: ${escapeHtml(accessibleTitle)}">Start upload</button>`);
  if (canAddAutoYoutubePlaylist) actionButtons.push(`<button type="button" class="primary queue-item-action" data-queue-action="add-auto-youtube-playlist" data-job-id="${escapeHtml(item.job.id)}" data-part-count="${escapeHtml(playlistStatus.part_count || bundleStates.length)}" aria-label="Add ${escapeHtml(accessibleTitle)} to its YouTube playlist">Add to playlist</button>`);
  if (canRecoverUncertainAutoYoutube) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="recover-auto-youtube" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Retry YouTube upload after review: ${escapeHtml(accessibleTitle)}">Retry upload</button>`);
  if (capabilities.can_cancel) actionButtons.push(`<button type="button" class="danger-outline queue-item-action" data-queue-action="cancel" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Cancel ${escapeHtml(accessibleTitle)}">Cancel</button>`);
  if (capabilities.can_stop_after_current) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="stop" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Stop Queue after ${escapeHtml(accessibleTitle)}">Stop after current</button>`);
  if (capabilities.can_remove) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="remove" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Remove ${escapeHtml(accessibleTitle)} from Queue">Remove from Queue</button>`);
  if (capabilities.can_retry) actionButtons.push(`<button type="button" class="quiet-button queue-item-action" data-queue-action="retry" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}" aria-label="Retry ${escapeHtml(status)}: ${escapeHtml(accessibleTitle)}">Retry</button>`);
  if ((item.state === 'error' || item.state === 'interrupted') && !item.resolved && capabilities.can_resolve !== false) actionButtons.push(`<button type="button" class="quiet-button queue-resolve-error" data-job-id="${escapeHtml(item.job.id)}" data-item-id="${escapeHtml(itemId)}">Mark as resolved</button>`);
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
      <div class="queue-row-identity"><div class="queue-streamer-identity">${streamerAvatarForKnownIdentity(item.streamer, 'queue')}<strong>${escapeHtml(item.streamer || (item.job?.type === 'recording' ? 'Twitch recording' : 'Unknown streamer'))}</strong></div><span>${escapeHtml(item.date || queueJobTypeLabel(item))}</span></div>
      <div class="queue-row-title">${escapeHtml(item.title || item.job.label)}</div>
      ${item.distinguishingLabel ? `<div class="queue-row-disambiguator muted">${escapeHtml(item.distinguishingLabel)}</div>` : ''}
      <span class="pill ${pillClass}">${escapeHtml(status)}</span>
      ${support}${cleanupNote}${reviewRequired}${retryRelationship}${actions}${details}
    </article>`;
  }
  return `<article class="queue-vod-item ${attention} ${presentation.reviewRequired ? 'is-uncertain' : ''}">
    <div class="queue-vod-main"><div class="queue-vod-copy">${identity ? `<div class="queue-vod-identity queue-streamer-identity">${streamerAvatarForKnownIdentity(item.streamer, 'queue')}<span>${escapeHtml(identity)}</span></div>` : ''}<strong>${escapeHtml(item.title || item.job.label)}</strong></div><span class="pill ${pillClass}">${escapeHtml(status)}</span></div>
    ${support}${error}${cleanupNote}${reviewRequired}${retryRelationship}${progress}${progressDetails}${actions}${details}
  </article>`;
}

function renderQueueGroup(id, items, emptyMessage, compact=false) {
  const box = $(id);
  if (!box) return;
  box.classList.toggle('muted', !items.length);
  box.innerHTML = items.length ? items.map(item => renderQueueVodItem(item, compact)).join('') : escapeHtml(emptyMessage);
  wireStreamerAvatarFallbacks(box);
  wireQueueItemInteractions(box);
}

function wireQueueItemInteractions(box) {
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
    'start-auto-youtube': ['/api/jobs/auto-youtube/release', 'Starting YouTube upload...'],
    'add-auto-youtube-playlist': ['/api/jobs/auto-youtube/playlist', 'Adding to YouTube playlist...'],
    'recover-auto-youtube': ['/api/jobs/auto-youtube/recover-uncertain', 'Requeuing reviewed YouTube upload...'],
  };
  box.querySelectorAll('.queue-item-action').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.queueAction;
    const route = actionRoutes[action];
    if (!route) return;
    const pendingKey = ['start-auto-youtube', 'add-auto-youtube-playlist', 'recover-auto-youtube'].includes(action)
      ? [button.dataset.jobId, action === 'recover-auto-youtube' ? button.dataset.itemId : ''].filter(Boolean).join(':')
      : '';
    const pendingActions = action === 'add-auto-youtube-playlist'
      ? pendingAutoYoutubePlaylistActions
      : action === 'recover-auto-youtube'
        ? pendingAutoYoutubeRecoveries
        : pendingAutoYoutubeReleases;
    if (pendingKey && pendingActions.has(pendingKey)) return;
    if (['start-auto-youtube', 'add-auto-youtube-playlist', 'recover-auto-youtube'].includes(action)) {
      const partCount = Number(button.dataset.partCount);
      const partNote = Number.isInteger(partCount) && partCount > 1
        ? `\nThis VOD contains ${partCount} parts.`
        : '';
      const question = action === 'add-auto-youtube-playlist'
        ? `Add this uploaded video to its YouTube playlist now?${partNote}`
        : action === 'recover-auto-youtube'
          ? 'Only retry after checking YouTube Studio. If the video exists there, retrying may create a duplicate.\n\nConfirm that no valid upload remains and any incomplete entry was deleted.'
          : `Start this YouTube upload now?${partNote}`;
      const confirmed = await confirmAction({
        title:action === 'add-auto-youtube-playlist'
          ? 'Add to YouTube playlist'
          : action === 'recover-auto-youtube'
            ? 'Retry uncertain upload?'
            : 'Start YouTube upload',
        message:question,
        confirmLabel:action === 'add-auto-youtube-playlist'
          ? 'Add to playlist'
          : action === 'recover-auto-youtube'
            ? 'I checked — retry upload'
            : 'Start upload'
      });
      if (!confirmed) return;
      pendingActions.add(pendingKey);
    }
    button.disabled = true;
    showToast(route[1]);
    try {
      const payload = ['start-auto-youtube', 'add-auto-youtube-playlist'].includes(action)
        ? {job_id:button.dataset.jobId}
        : action === 'recover-auto-youtube'
          ? {job_id:button.dataset.jobId, item_id:button.dataset.itemId, reviewed:true}
          : {job_id:button.dataset.jobId, item_id:button.dataset.itemId};
      const result = await api(route[0], {method:'POST', body:JSON.stringify(payload)});
      if (action === 'retry' && result.retry_job_id) showToast(`Retry started as Job ${result.retry_job_id}.`);
      if (action === 'start-auto-youtube') showToast('YouTube upload queued.');
      if (action === 'add-auto-youtube-playlist') showToast('YouTube playlist updated.');
      if (action === 'recover-auto-youtube') showToast('Reviewed upload requeued.');
      await pollJobs();
    } catch (error) {
      button.disabled = false;
      showToast(friendlyQueueActionError(error), 'bad');
    } finally {
      if (pendingKey) pendingActions.delete(pendingKey);
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
    invalid_request:'Select one valid Auto YouTube job to start.',
    release_not_allowed:'This Auto YouTube job is no longer waiting for manual start.',
    conflicting_ownership:'This Auto YouTube job cannot start because its ownership state conflicts.',
    ownership_mismatch:'This Auto YouTube job cannot start because its ownership state is inconsistent.',
    release_media_invalid:'The prepared upload media is no longer valid.',
    job_store_unavailable:'Job history persistence is unavailable. No upload was started.',
    ownership_store_unavailable:'Auto YouTube upload state is unavailable. No upload was started.',
    release_worker_start_failed:'The upload worker could not be started. Try again.',
    playlist_not_pending:'This Auto YouTube job is not ready for playlist insertion.',
    playlist_lookup_failed:'YouTube playlist membership could not be checked.',
    playlist_persistence_failed:'Playlist state could not be saved safely. No duplicate insert was attempted.',
    needs_attention:'Playlist membership needs review before another action.',
    review_confirmation_required:'Confirm that you checked YouTube Studio before retrying this upload.',
    video_already_confirmed:'This part already has a confirmed YouTube video and cannot be retried.',
    recovery_not_allowed:'This upload is no longer eligible for uncertain-upload recovery.',
    recovery_media_invalid:'The prepared upload media is no longer available or has changed.',
    recovery_persistence_failed:'The reviewed recovery could not be saved safely. No upload was started.',
    recovery_worker_start_failed:'The reviewed upload was saved but its worker could not be started. Try again.',
  };
  return messages[String(error?.reason || '')] || String(error?.message || 'The Queue action could not be completed.');
}

function queueLaneControlView(control={}, known=false) {
  const paused = control?.queue_paused === true;
  return {
    paused,
    note:!known
      ? 'Lane control unavailable'
      : paused
        ? (control.stop_after_current && control.has_active_item ? 'Stops after current' : 'Paused')
        : 'Processing enabled',
    action:paused ? 'resume' : 'pause',
    label:paused ? 'Resume Queue' : 'Pause Queue',
  };
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
    const known = Object.prototype.hasOwnProperty.call(queueControls, lane);
    const view = queueLaneControlView(control, known);
    return `<div class="queue-lane-control"><span><strong>${label}</strong><small>${escapeHtml(view.note)}</small></span><button type="button" class="quiet-button queue-lane-action" data-lane="${lane}" data-action="${view.action}"${known ? '' : ' disabled'}>${view.label}</button></div>`;
  }).join('');
  box.querySelectorAll('.queue-lane-action').forEach(button => button.addEventListener('click', () => {
    const action = button.dataset.action;
    return withButtonPending(button, {pendingLabel:action === 'pause' ? 'Pausing...' : 'Resuming...'}, async () => {
      await api(`/api/queue/${action}`, {method:'POST', body:JSON.stringify({lane:button.dataset.lane})});
      await pollJobs();
    }).catch(error => showToast(error.message, 'bad'));
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

function queueOperationsView(items, queueControls={}) {
  const active = (items || []).filter(item => item.state === 'running' || item.state === 'cancelling');
  const waiting = (items || []).filter(item => item.state === 'waiting');
  const errors = (items || []).filter(item => (item.state === 'error' || item.state === 'interrupted') && !item.resolved);
  const lane = type => ({
    active:active.filter(item => item.job?.type === type),
    waiting:waiting.filter(item => item.job?.type === type),
    control:queueControls?.[type] || null,
  });
  return {active, waiting, errors, download:lane('download'), upload:lane('youtube_upload')};
}

function setQueueWorkspaceVisibility(id, visible) {
  const element = $(id);
  if (element) element.hidden = !visible;
}

function setQueueWorkspaceCount(id, value) {
  const element = $(id);
  if (element) element.textContent = String(value);
}

function queueLaneSummary(label, lane) {
  const paused = lane.control?.queue_paused === true;
  const knownControl = lane.control !== null;
  const state = paused ? 'Paused' : lane.active.length ? 'Running' : lane.waiting.length ? 'Waiting' : knownControl ? 'Idle' : 'Status unavailable';
  const detail = knownControl
    ? `${lane.active.length} running · ${lane.waiting.length} waiting`
    : `${lane.active.length} running · ${lane.waiting.length} waiting · Lane status unavailable`;
  return `<article class="queue-summary-card is-${paused ? 'paused' : lane.active.length ? 'running' : lane.waiting.length ? 'waiting' : 'idle'}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(state)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function renderQueueOperationalSummary(view) {
  const box = $('queueOperationalSummary');
  if (!box) return;
  const reviewTitle = view.errors.length ? `${view.errors.length} need review` : 'None';
  const activeDetail = view.active.length
    ? `${view.download.active.length} download · ${view.upload.active.length} upload`
    : 'No active processes';
  box.innerHTML = [
    queueLaneSummary('Downloads', view.download),
    queueLaneSummary('Uploads', view.upload),
    `<article class="queue-summary-card is-${view.active.length ? 'running' : 'idle'}"><span>Running</span><strong>${view.active.length ? `${view.active.length} active` : 'Idle'}</strong><small>${escapeHtml(activeDetail)}</small></article>`,
    `<article class="queue-summary-card is-${view.errors.length ? 'attention' : 'idle'}"><span>Needs review</span><strong>${escapeHtml(reviewTitle)}</strong><small>${view.errors.length ? 'Action is required in Queue.' : 'No actionable Queue issues.'}</small></article>`,
  ].join('');
}

function renderQueueOperationLanes(view) {
  const hasRunning = view.active.length > 0;
  const hasWaiting = view.waiting.length > 0;
  $('queueRunningSection')?.classList.toggle('is-idle', !hasRunning);
  $('queueRunning')?.classList.toggle('is-idle', !hasRunning);
  const activeCount = $('queueActive');
  if (activeCount) activeCount.hidden = !hasRunning;
  setQueueWorkspaceVisibility('queueRunningDownloadsLane', view.download.active.length > 0);
  setQueueWorkspaceVisibility('queueRunningUploadsLane', view.upload.active.length > 0);
  setQueueWorkspaceVisibility('queueRunningIdle', !hasRunning && hasWaiting);
  setQueueWorkspaceCount('queueRunningDownloadsCount', view.download.active.length);
  setQueueWorkspaceCount('queueRunningUploadsCount', view.upload.active.length);
  renderQueueGroup('queueRunningDownloads', view.download.active, '', false);
  renderQueueGroup('queueRunningUploads', view.upload.active, '', false);

  setQueueWorkspaceVisibility('queueWaitingSection', hasWaiting);
  setQueueWorkspaceVisibility('queueWaitingDownloadsLane', view.download.waiting.length > 0);
  setQueueWorkspaceVisibility('queueWaitingUploadsLane', view.upload.waiting.length > 0);
  setQueueWorkspaceCount('queueWaitingDownloadsCount', view.download.waiting.length);
  setQueueWorkspaceCount('queueWaitingUploadsCount', view.upload.waiting.length);
  renderQueueGroup('queueWaitingDownloads', view.download.waiting, '', true);
  renderQueueGroup('queueWaitingUploads', view.upload.waiting, '', true);
}

function renderVodQueue(jobs, queueControls={}, persistenceStatus={}) {
  const items = queueItemsFromJobs(jobs);
  const operations = queueOperationsView(items, queueControls);
  const {active:running, waiting} = operations;
  const errors = distinguishQueueItems(queueHistoryNewest(operations.errors));
  const completed = distinguishQueueItems(queueHistoryNewest(items.filter(item => item.state === 'completed')));
  const cancelled = distinguishQueueItems(queueHistoryNewest(items.filter(item => item.state === 'cancelled')));
  document.querySelector('#page-queue .completed-section')?.classList.toggle('is-empty', completed.length === 0);
  renderQueueOperationalSummary({...operations, errors});
  renderQueueOperationLanes({...operations, errors});
  renderQueueGroup('queueErrors', errors, 'No jobs need attention.', true);
  renderQueueGroup('queueCompleted', completed, 'No completed jobs.', true);
  renderQueueGroup('queueCancelled', cancelled, 'No cancelled jobs.', true);
  renderQueueLaneControls(queueControls);
  renderQueuePersistenceStatus(persistenceStatus);
  renderOverallRunningEstimate(running);
  setQueueWorkspaceCount('queueActive', running.length);
  setQueueWorkspaceCount('queueWaitingCount', waiting.length);
  setQueueWorkspaceCount('queueFailed', errors.length);
  if ($('queueDone')) $('queueDone').textContent = String(completed.length);
  const hasEligibleAutoYoutubePlaylist = completed.some(item => (
    item.job?.type === 'youtube_upload'
    && item.job?.origin === 'auto_youtube'
    && item.job?.auto_youtube_playlist?.state === 'playlist_pending'
    && item.job?.auto_youtube_playlist?.eligible === true
  ));
  if (!hasEligibleAutoYoutubePlaylist) {
    autoYoutubePlaylistHistoryAutoOpened = false;
  } else if (!autoYoutubePlaylistHistoryAutoOpened) {
    const history = $('queueCompletedDetails');
    if (history) history.open = true;
    autoYoutubePlaylistHistoryAutoOpened = true;
  }
  setQueueWorkspaceCount('queueCancelledCount', cancelled.length);
  if ($('clearCompletedJobs')) {
    $('clearCompletedJobs').disabled = completed.length === 0;
    $('clearCompletedJobs').classList.toggle('hidden', completed.length === 0);
  }
  setQueueWorkspaceVisibility('queueCancelledSection', cancelled.length > 0);
  setQueueWorkspaceVisibility('queueErrorsSection', errors.length > 0);
  return {items, running, waiting, errors, completed, cancelled, operations};
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


function dashboardStatusKind(kind) {
  return ({running:'healthy', starting:'checking', paused:'paused', degraded:'attention', unavailable:'unavailable', loading:'loading'})[kind] || 'loading';
}

function setDashboardStatus(cardId, statusId, view) {
  const card = $(cardId);
  const box = $(statusId);
  if (!card || !box) return;
  const kind = view.kind || 'loading';
  const statusKind = dashboardStatusKind(kind);
  card.dataset.status = statusKind;
  const dot = card.querySelector('.dashboard-status-dot');
  if (dot) dot.className = `dashboard-status-dot is-${statusKind}`;
  box.className = `dashboard-status-copy is-${statusKind}`;
  box.innerHTML = `<strong>${escapeHtml(view.title || 'Checking…')}</strong><span>${escapeHtml(view.detail || '')}</span>`;
}

function dashboardVodAutomationView(snapshot) {
  if (!snapshot) return {kind:'loading', title:'Checking…', detail:'Loading monitor status.', metrics:[]};
  const status = autoVodStatusPresentation(snapshot);
  const kind = status.kind === 'running' && snapshot.in_progress ? 'starting' : status.kind;
  const title = ({running:'Healthy', starting:'Checking…', paused:'Paused', degraded:'Needs attention', unavailable:'Unavailable'})[kind] || 'Checking…';
  const metrics = [];
  if (snapshot.initialized === true && Number.isFinite(Number(snapshot.watched_count))) metrics.push(`${Number(snapshot.watched_count)} monitored`);
  const summary = automationProductView().summary || {};
  const downloadAndYoutube = Number(summary.download_and_youtube) || 0;
  const downloadOnly = Number(summary.auto_download) || 0;
  const needsReview = Number(summary.needs_review) || 0;
  if (downloadAndYoutube) metrics.push(`${downloadAndYoutube} Download + YouTube`);
  if (downloadOnly) metrics.push(`${downloadOnly} Auto Download`);
  if (needsReview) metrics.push(`${needsReview} Needs Review`);
  return {kind, title, detail:status.detail, metrics};
}

function renderDashboardVodAutomation(snapshot=autoVodStatusSnapshot) {
  const view = dashboardVodAutomationView(snapshot);
  setDashboardStatus('dashboardVodAutomationCard', 'autoVodStatus', view);
  const metrics = $('dashboardVodAutomationMetrics');
  if (metrics) metrics.innerHTML = (view.metrics || []).map(metric => `<span>${escapeHtml(metric)}</span>`).join('');
}

function dashboardLiveRecordingView(snapshot=autoRecorderStatusSnapshot) {
  if (!snapshot) return {kind:'loading', title:'Checking…', detail:'Loading monitor status.', metrics:[]};
  const status = autoRecorderStatusPresentation(snapshot);
  const kind = status.kind === 'running' ? 'running' : status.kind;
  const title = ({running:'Healthy', starting:'Checking…', paused:'Paused', degraded:'Needs attention', unavailable:'Unavailable', loading:'Checking…'})[kind] || 'Checking…';
  const metrics = [];
  if (snapshot.unavailable !== true && Number.isFinite(Number(snapshot.watched_count))) metrics.push(`${Number(snapshot.watched_count)} monitored`);
  const recordings = liveRecordingJobs.filter(job => ACTIVE_RECORDING_STATES.has(job?.state)).length;
  metrics.push(`${recordings} recording now`);
  return {kind, title, detail:status.detail, metrics};
}

function renderDashboardLiveRecording(snapshot=autoRecorderStatusSnapshot) {
  const view = dashboardLiveRecordingView(snapshot);
  setDashboardStatus('dashboardLiveRecordingCard', 'dashboardLiveRecordingStatus', view);
  const metrics = $('dashboardLiveRecordingMetrics');
  if (metrics) metrics.innerHTML = (view.metrics || []).map(metric => `<span>${escapeHtml(metric)}</span>`).join('');
}

function dashboardQueueView(queue) {
  const operational = (queue || []).filter(item => item.job?.type !== 'recording');
  const active = operational.filter(item => item.state === 'running' || item.state === 'cancelling');
  const waiting = operational.filter(item => item.state === 'waiting');
  const unresolvedErrors = operational.filter(item => item.state === 'error' && !item.resolved);
  const errors = [...unresolvedErrors, ...operational.filter(item => item.state === 'interrupted' && !item.resolved)];
  const downloads = active.filter(item => item.job?.type === 'download').length;
  const uploads = active.filter(item => item.job?.type === 'youtube_upload').length;
  const title = errors.length ? 'Needs attention' : active.length ? `${active.length} running` : waiting.length ? 'Waiting' : 'Healthy';
  const detail = errors.length ? `${errors.length} item${errors.length === 1 ? '' : 's'} need review.` : active.length ? 'Queue is processing work.' : waiting.length ? `${waiting.length} item${waiting.length === 1 ? '' : 's'} waiting to start.` : 'No active or waiting work.';
  const metrics = [];
  if (downloads) metrics.push(`${downloads} download${downloads === 1 ? '' : 's'}`);
  if (uploads) metrics.push(`${uploads} upload${uploads === 1 ? '' : 's'}`);
  if (waiting.length) metrics.push(`${waiting.length} waiting`);
  if (errors.length) metrics.push(`${errors.length} error${errors.length === 1 ? '' : 's'}`);
  return {kind:errors.length ? 'degraded' : 'running', title, detail, metrics, active, waiting, errors};
}

function dashboardYoutubeView(youtube={}) {
  if (youtube.google_libs_available === false) return {kind:'unavailable', title:'Unavailable', detail:'Required YouTube libraries are not available.', metrics:[]};
  if (youtube.connected === true) {
    const metrics = state?.settings?.youtube_privacy_status ? [`Default ${state.settings.youtube_privacy_status}`] : [];
    return {kind:'running', title:'Connected', detail:youtube.channel_title ? `Connected as ${youtube.channel_title}` : 'Connection is ready.', metrics};
  }
  const enabled = state?.settings?.youtube_enabled === true;
  return {kind:enabled ? 'degraded' : 'paused', title:enabled ? 'Needs attention' : 'Not connected', detail:enabled ? 'Connect YouTube before starting an upload.' : 'Connect YouTube when uploads are needed.', metrics:[]};
}

function renderDashboardSystemOverview(data={}, queue=[]) {
  const queueView = dashboardQueueView(queue);
  setDashboardStatus('dashboardQueueCard', 'dashboardQueueStatus', queueView);
  const queueMetrics = $('dashboardQueueMetrics');
  if (queueMetrics) queueMetrics.innerHTML = queueView.metrics.map(metric => `<span>${escapeHtml(metric)}</span>`).join('');
  const youtubeView = dashboardYoutubeView(data.youtube || {});
  setDashboardStatus('dashboardYoutubeCard', 'dashboardYoutubeStatus', youtubeView);
  const youtubeMetrics = $('dashboardYoutubeMetrics');
  if (youtubeMetrics) youtubeMetrics.innerHTML = youtubeView.metrics.map(metric => `<span>${escapeHtml(metric)}</span>`).join('');
  const disk = data.disk || {};
  const storageCard = $('dashboardStorageCard');
  if (storageCard) {
    const free = Number(disk.free_gb), total = Number(disk.total_gb);
    const available = disk.ok === true && Number.isFinite(free) && Number.isFinite(total);
    storageCard.hidden = !available;
    if (available) {
      const low = free < 50;
      setDashboardStatus('dashboardStorageCard', 'dashboardStorageStatus', {
        kind:low ? 'degraded' : 'running', title:low ? 'Low space' : 'Available', detail:`${free.toFixed(1)} GB free of ${total.toFixed(1)} GB`
      });
    }
  }
  renderDashboardVodAutomation();
  renderDashboardLiveRecording();
  return queueView;
}

function dashboardLifecycleHtml(item) {
  const playlist = item.job?.auto_youtube_playlist;
  const cleanup = item.job?.auto_youtube_cleanup;
  if (item.job?.origin !== 'auto_youtube' || (!playlist && !cleanup)) return '';
  const uploadState = item.state === 'completed' ? 'complete' : 'current';
  const playlistLabel = playlist?.state === 'playlist_added' ? 'Playlist added' : playlist?.state === 'needs_attention' ? 'Playlist review' : playlist?.state === 'playlist_pending' ? 'Playlist pending' : '';
  const cleanupLabel = cleanup?.state === 'removed' ? 'Local copy removed' : cleanup?.state === 'scheduled' ? 'Cleanup scheduled' : cleanup?.state === 'needs_attention' ? 'Cleanup review' : '';
  return `<div class="dashboard-lifecycle" aria-label="Automatic YouTube lifecycle"><span class="is-${uploadState}">YouTube upload</span>${playlistLabel ? `<span class="is-${playlist?.state === 'needs_attention' ? 'attention' : 'pending'}">${escapeHtml(playlistLabel)}</span>` : ''}${cleanupLabel ? `<span class="is-${cleanup?.state === 'needs_attention' ? 'attention' : 'pending'}">${escapeHtml(cleanupLabel)}</span>` : ''}</div>`;
}

function dashboardActivityCardHtml(item) {
  const lane = item.job?.type === 'youtube_upload' ? 'Upload' : 'Download';
  const progress = Number(item.progress);
  const hasProgress = Number.isFinite(progress);
  return `<article class="dashboard-activity-card is-${lane.toLowerCase()}"><div class="dashboard-activity-card-head"><span class="dashboard-activity-lane">${lane}</span><span class="pill accent">${escapeHtml(item.operation || 'Running')}</span></div><strong>${escapeHtml(item.streamer || item.title || 'Current item')}</strong><span class="dashboard-activity-title">${escapeHtml(item.title || item.filename || item.job?.label || '')}</span>${hasProgress ? `<div class="dashboard-activity-progress"><span><strong>${Math.max(0, Math.min(100, progress)).toFixed(progress % 1 ? 1 : 0)}%</strong><small>${escapeHtml(item.extra || '')}</small></span><div class="progress-bar"><span style="width:${Math.max(0, Math.min(100, progress))}%"></span></div></div>` : `<span class="dashboard-activity-detail">${escapeHtml(item.extra || item.operation || 'Processing')}</span>`}${dashboardLifecycleHtml(item)}</article>`;
}

function dashboardCurrentActivityState(queueView={}) {
  const active = queueView.active || [];
  const waiting = queueView.waiting || [];
  return {active, waiting, idle:!active.length && !waiting.length};
}

function renderDashboardCurrentActivity(queueView) {
  const box = $('dashboardRunning');
  const count = $('dashboardActivityCount');
  if (!box) return;
  const section = $('dashboardRunningSection');
  const {active, waiting, idle} = dashboardCurrentActivityState(queueView);
  section?.classList.toggle('is-idle', idle);
  box.classList.toggle('muted', idle);
  box.classList.toggle('dashboard-activity-idle', idle);
  box.innerHTML = active.length ? active.slice(0, 2).map(dashboardActivityCardHtml).join('') : idle ? 'Nothing is running right now.' : 'No downloads or uploads are currently running.';
  if (count) {
    count.hidden = idle;
    count.textContent = active.length ? `· ${active.length} running` : waiting.length ? `· ${waiting.length} waiting` : '';
  }
  const upcoming = $('dashboardUpcomingSection');
  const upcomingBox = $('dashboardUpcoming');
  if (upcoming && upcomingBox) {
    upcoming.hidden = !waiting.length;
    upcomingBox.innerHTML = waiting.length ? waiting.slice(0, 3).map(item => `<span>${escapeHtml(item.operation || 'Waiting')} · ${escapeHtml(item.streamer || item.title || item.job?.label || 'Queue item')}</span>`).join('') : '';
  }
}

function dashboardAttentionIssues(queueView, data={}) {
  const issues = [];
  if (queueView.errors.length) issues.push({kind:'danger', title:`${queueView.errors.length} queue item${queueView.errors.length === 1 ? '' : 's'} need attention`, detail:queueView.errors[0].title || queueView.errors[0].job?.label || 'Review the affected Queue item.', page:'queue', label:'Open Queue'});
  const vod = dashboardVodAutomationView(autoVodStatusSnapshot);
  if (vod.kind === 'degraded') issues.push({kind:'warning', title:'VOD Automation needs attention', detail:vod.detail, page:'settings', target:'streamers', label:'Review Streamers'});
  const live = dashboardLiveRecordingView(autoRecorderStatusSnapshot);
  if (live.kind === 'degraded') issues.push({kind:'warning', title:'Live Recording needs attention', detail:live.detail, page:'live', label:'View Live'});
  const youtube = dashboardYoutubeView(data.youtube || {});
  if (youtube.kind === 'degraded') issues.push({kind:'warning', title:'YouTube is not connected', detail:youtube.detail, page:'settings', target:'youtube', label:'Open YouTube Settings'});
  const disk = data.disk || {};
  if (disk.ok === true && Number(disk.free_gb) < 50) issues.push({kind:'warning', title:'Storage is running low', detail:`${Number(disk.free_gb).toFixed(1)} GB is available for VOD downloads.`, page:'settings', target:'advanced', label:'Review Settings'});
  return issues;
}

function renderDashboardAttention(queueView, data={}) {
  const section = $('dashboardAttentionSection');
  const box = $('dashboardAlerts');
  if (!section || !box) return;
  const issues = dashboardAttentionIssues(queueView, data);
  section.hidden = !issues.length;
  if (!issues.length) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = issues.map(issue => `<article class="dashboard-attention-row is-${issue.kind}"><div><strong>${escapeHtml(issue.title)}</strong><span>${escapeHtml(issue.detail)}</span></div><button type="button" class="goto-page quiet-button" data-page="${escapeHtml(issue.page)}"${issue.target ? ` data-settings-target="${escapeHtml(issue.target)}"` : ''}>${escapeHtml(issue.label)}</button></article>`).join('');
}

function wireDashboardNavigation() {
  document.querySelectorAll('#page-dashboard .goto-page').forEach(button => button.onclick = () => {
    showPage(button.dataset.page);
    if (button.dataset.settingsTarget) showSettingsTab(button.dataset.settingsTarget);
  });
}

async function refreshDashboard() {
  try {
    const [data, jobsData] = await Promise.all([api('/api/dashboard'), api('/api/jobs')]);
    const queue = queueItemsFromJobs(jobsData.jobs || []);
    updateLiveRecordingJobs(jobsData.jobs || []);
    const queueView = renderDashboardSystemOverview(data, queue);
    renderDashboardCurrentActivity(queueView);
    renderDashboardAttention(queueView, data);
    wireDashboardNavigation();
  } catch (error) {
    const section = $('dashboardAttentionSection');
    const box = $('dashboardAlerts');
    if (section && box) {
      section.hidden = false;
      box.innerHTML = `<article class="dashboard-attention-row is-danger"><div><strong>Dashboard status could not be loaded</strong><span>${escapeHtml(error.message)}</span></div></article>`;
    }
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
  const actions = $('localSelectionActions');
  if (actions) actions.hidden = selected === 0;
  const summary = $('localSelectionSummary');
  if (summary) summary.textContent = `${selected} VOD${selected === 1 ? '' : 's'} selected`;
  if (uploadBtn) uploadBtn.textContent = selected ? `Upload ${selected} VOD${selected === 1 ? '' : 's'}` : 'Upload Selected';
}

function updateLocalBulkSelectionControl(hasSelectable) {
  const selectReady = $('checkAllLocalVideos');
  if (!selectReady) return;
  selectReady.hidden = !hasSelectable;
  selectReady.disabled = !hasSelectable;
}

function workspaceStatusClass(video) {
  if (video.auto_youtube_managed) return video.auto_youtube_video_confirmed ? 'good' : 'accent';
  if (video.local_file_exists === false) return 'warn';
  if (video.already_uploaded) return 'good';
  if (video.in_uploaded_folder) return 'accent';
  if (video.manually_uploaded || video.dashboard_uploaded) return 'good';
  if (video.prepared) return 'warn';
  return 'muted';
}

function workspaceStatusLabel(video) {
  if (video.auto_youtube_managed) return video.auto_youtube_status || 'Managed by Auto YouTube';
  return video.status || 'Ready';
}

function localVideoByPath(path) {
  return localVideoCache.get(path) || null;
}

function localCleanupLabel(video) {
  const cleanup = video.auto_youtube_cleanup;
  if (!cleanup) return '';
  if (cleanup.state === 'disabled') return 'Local cleanup off';
  if (cleanup.state === 'waiting_for_upload') return 'Local cleanup waits for upload';
  if (cleanup.state === 'keep_local') return 'Local copy kept';
  if (cleanup.state === 'due') return 'Ready for local cleanup';
  if (cleanup.state === 'cleaning') return 'Removing local copy';
  if (cleanup.state === 'removed') return 'Local copy removed';
  if (cleanup.state === 'needs_attention') return 'Local cleanup needs attention';
  if (cleanup.state === 'local_copy_missing') return 'Local copy unavailable';
  if (cleanup.state === 'scheduled' && cleanup.cleanup_due_at) {
    const seconds = Math.max(0, (Date.parse(cleanup.cleanup_due_at) - Date.now()) / 1000);
    return `Local cleanup in ${formatRemainingDuration(seconds).replace(' remaining', '')}`;
  }
  return '';
}

function localVideoFilterState(video) {
  const cleanupState = video?.auto_youtube_cleanup?.state;
  if (cleanupState === 'needs_attention') return 'attention';
  if (['scheduled', 'due', 'cleaning', 'removed', 'local_copy_missing'].includes(cleanupState)) return 'cleanup';
  if (video?.already_uploaded) return 'uploaded';
  if (video?.auto_youtube_managed) return 'automatic';
  return 'ready';
}

function localVideoRowsForView(videos, includeUploaded, filter, historyLimit=UPLOADED_HISTORY_PAGE_SIZE) {
  const visible = visibleLocalVideoRows(videos, includeUploaded, historyLimit);
  return filter === 'all' ? visible : visible.filter(video => localVideoFilterState(video) === filter);
}

function localEmptyStateCopy(filter, includeUploaded) {
  if (filter === 'ready') return 'No local VODs are ready for manual upload.';
  if (filter === 'automatic') return 'No local VODs are managed by the automatic upload lifecycle.';
  if (filter === 'cleanup') return 'No local VODs have cleanup scheduled.';
  if (filter === 'attention') return 'No local VODs need attention.';
  if (includeUploaded) return 'No matching local VODs or uploaded history found.';
  return 'No local VODs found.';
}

function renderLocalVideoCard(v) {
  const uploaded = !!v.already_uploaded;
  const hasLocalFile = v.local_file_exists !== false;
  const autoYouTubeManaged = !!v.auto_youtube_managed;
  const uploadable = !v.already_uploaded && !autoYouTubeManaged && hasLocalFile;
  const statusClassName = workspaceStatusClass(v);
  const secondaryStatus = autoYouTubeManaged
    ? (v.auto_youtube_video_confirmed ? 'Uploaded to YouTube' : 'Automatic upload lifecycle')
    : uploaded
    ? 'Uploaded to YouTube'
    : (v.prepared ? 'Metadata ready' : 'Metadata needed');
  const cleanup = v.auto_youtube_cleanup || {};
  const cleanupLabel = localCleanupLabel(v);
  const cleanupAction = cleanup.can_keep_local
    ? `<button type="button" class="video-action" data-action="keep-local" data-path="${escapeHtml(v.path)}">Keep local</button>`
    : cleanup.can_resume_cleanup
    ? `<button type="button" class="video-action" data-action="resume-cleanup" data-path="${escapeHtml(v.path)}">Allow automatic cleanup</button>`
    : '';
  const workflow = autoYouTubeManaged ? 'Automatic' : uploaded ? 'History' : 'Manual';
  const localState = cleanupLabel || (hasLocalFile
    ? (uploaded ? 'Local copy available' : 'Local copy available')
    : 'Local copy removed');
  return `<article class="video-workspace-card ${uploaded ? 'is-uploaded' : ''} ${hasLocalFile ? '' : 'is-local-removed'}" data-video-path="${escapeHtml(v.path)}">
    ${uploadable ? `<label class="video-select"><input class="localvideocheck" type="checkbox" data-path="${escapeHtml(v.path)}" checked><span>Select</span></label>` : `<span class="video-select muted">${autoYouTubeManaged ? 'Automatic' : 'History'}</span>`}
    <div class="video-person"><div class="video-streamer-identity">${streamerAvatarForKnownIdentity(v.streamer, 'local')}<strong>${escapeHtml(v.streamer || 'Unknown streamer')}</strong></div><span>${escapeHtml(v.date_de || 'Unknown date')}</span></div>
    <strong class="video-display-title">${escapeHtml(v.title || v.youtube_title || v.name)}</strong>
    <span class="video-size">${hasLocalFile ? `${escapeHtml(v.size_gb)} GB` : 'Size unavailable'}</span>
    <span class="video-workflow ${autoYouTubeManaged ? 'accent' : 'muted'}">${workflow}<small>${escapeHtml(workspaceStatusLabel(v))}</small></span>
    <span class="metadata-status ${statusClassName}">${escapeHtml(localState)}<small class="auto-youtube-cleanup-status">${escapeHtml(secondaryStatus)}</small></span>
    <div class="video-primary-actions">${uploadable ? `<button type="button" class="primary video-action" data-action="upload" data-path="${escapeHtml(v.path)}">Upload</button>` : ''}</div>
    ${hasLocalFile ? `<details class="technical-details secondary-actions"><summary>Actions</summary><div class="video-copy-actions"><button type="button" class="video-action" data-action="copy-title" data-path="${escapeHtml(v.path)}">Copy Title</button><button type="button" class="video-action" data-action="copy-description" data-path="${escapeHtml(v.path)}">Copy Description</button>${cleanupAction}${uploadable && !v.prepared ? `<button type="button" class="video-action" data-action="prepare" data-path="${escapeHtml(v.path)}">Prepare metadata</button>` : ''}${uploadable ? `<button type="button" class="video-action" data-action="mark" data-path="${escapeHtml(v.path)}">Mark as Uploaded</button>` : ''}</div><div class="danger-zone"><strong>Delete the local VOD file and its sidecars</strong><button type="button" class="danger-outline video-action" data-action="delete" data-path="${escapeHtml(v.path)}">Delete Permanently</button></div></details>` : '<span class="muted">Upload history retained; local actions are unavailable.</span>'}
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
  const box = $('localVideoCards');
  const info = $('localVideosInfo');
  const errorBox = $('localVideosError');
  if (!box) return;
  try {
    const data = await api('/api/local-videos?include_uploaded=' + (includeUploaded ? '1' : '0'));
    const videos = Array.isArray(data.videos) ? data.videos.filter(video => video && typeof video === 'object') : [];
    const counts = data.counts || {};
    const filter = $('localVodsFilter')?.value || 'all';
    localVideoCache = new Map(videos.filter(video => video.path).map(video => [video.path, video]));
    const visibleVideos = localVideoRowsForView(videos, includeUploaded, filter, uploadedHistoryVisibleCount);
    const uploadedCount = videos.filter(video => video.already_uploaded).length;
    const hiddenUploadedCount = includeUploaded && filter === 'all'
      ? Math.max(0, uploadedCount - uploadedHistoryVisibleCount)
      : 0;

    if (errorBox) { errorBox.hidden = true; errorBox.textContent = ''; }
    if ($('workspacePending')) $('workspacePending').textContent = String(counts.pending || 0);
    if (info) {
      const automatic = videos.filter(video => video.auto_youtube_managed && !video.already_uploaded).length;
      info.hidden = !visibleVideos.length;
      info.textContent = visibleVideos.length
        ? (includeUploaded
          ? `${counts.pending || 0} ready · ${automatic} automatic · ${counts.uploaded || 0} uploaded or archived`
          : `${counts.pending || 0} ready · ${automatic} automatic`)
        : '';
      info.className = 'inline-status muted';
    }

    if (!visibleVideos.length) {
      box.innerHTML = `<div class="empty-workspace muted">${escapeHtml(localEmptyStateCopy(filter, includeUploaded))}</div>`;
      updateLocalBulkSelectionControl(false);
      updateLocalUploadButton();
      return;
    }

    box.innerHTML = visibleVideos.map(renderLocalVideoCard).join('') + (
      hiddenUploadedCount
        ? `<button type="button" id="showMoreUploadedHistory" class="quiet-button show-more-upload-history">Show more · ${hiddenUploadedCount} older upload${hiddenUploadedCount === 1 ? '' : 's'}</button>`
        : ''
    );
    wireStreamerAvatarFallbacks(box);
    box.querySelectorAll('.localvideocheck').forEach(cb => cb.addEventListener('change', updateLocalUploadButton));
    box.querySelectorAll('.video-action').forEach(btn => btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      const path = btn.dataset.path;
      if (action === 'upload') {
        withButtonPending(btn, {pendingLabel:'Adding...'}, () => handleLocalVideoAction(action, path))
          .catch(error => showToast(error.message || 'The VOD could not be added to the upload queue.', 'bad'));
        return;
      }
      handleLocalVideoAction(action, path);
    }));
    const showMore = $('showMoreUploadedHistory');
    if (showMore) showMore.addEventListener('click', () => {
      uploadedHistoryVisibleCount += UPLOADED_HISTORY_PAGE_SIZE;
      loadLocalVideos().catch(error => alert(error.message));
    });
    updateLocalBulkSelectionControl(box.querySelectorAll('.localvideocheck').length > 0);
    updateLocalUploadButton();
  } catch (error) {
    localVideoCache = new Map();
    if (info) { info.hidden = false; info.textContent = 'Local media could not be loaded.'; }
    if (errorBox) { errorBox.hidden = false; errorBox.textContent = error.message || 'Unable to load local media.'; }
    box.innerHTML = '<div class="empty-workspace muted">Local media could not be loaded. Refresh to try again.</div>';
    updateLocalBulkSelectionControl(false);
    updateLocalUploadButton();
    throw error;
  }
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
    const confirmed = await confirmAction({
      title:'Mark upload complete',
      message:`Has the YouTube upload completed?\n\n${video.name}\n\nThe VOD will be marked as manually uploaded.`,
      confirmLabel:'Mark as uploaded'
    });
    if (!confirmed) return;
    await api('/api/local-video/mark-uploaded', { method:'POST', body: JSON.stringify({ path }) });
    showToast('Marked as manually uploaded.');
    await loadLocalVideos();
    return;
  }

  if (action === 'keep-local' || action === 'resume-cleanup') {
    const keepLocal = action === 'keep-local';
    await api('/api/auto-youtube/cleanup/keep-local', {
      method:'POST',
      body: JSON.stringify({
        streamer: video.auto_youtube_streamer,
        twitch_vod_id: video.auto_youtube_twitch_vod_id,
        media_path: path,
        keep_local: keepLocal
      })
    });
    showToast(keepLocal ? 'Local copy will be kept.' : 'Automatic cleanup rescheduled with a fresh delay.');
    await loadLocalVideos();
    return;
  }

  if (action === 'delete') {
    const confirmed = await confirmAction({
      title:'Delete local VOD',
      message:`Permanently delete this VOD and its matching TXT/JSON files?\n\n${video.name}\n\nThe files will not be moved to the recycle bin or trash.`,
      confirmLabel:'Delete VOD',
      variant:'danger'
    });
    if (!confirmed) return;
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
    if (!streamerPolicyEditorDirty) renderStreamerEditor();
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

$('presetToday').addEventListener('click', () => setDatePreset('today'));
$('presetYesterdayToday').addEventListener('click', () => setDatePreset('yesterday-today'));
$('preset7').addEventListener('click', () => setDatePreset('last-7'));
$('preset30').addEventListener('click', () => setDatePreset('last-30'));
$('presetCustom').addEventListener('click', () => setDatePreset('custom'));
$('fromDate').addEventListener('change', () => setDatePreset('custom'));
$('toDate').addEventListener('change', () => setDatePreset('custom'));
$('searchStreamerPickerToggle').addEventListener('click', toggleSearchStreamerPicker);
$('searchStreamerFilter').addEventListener('input', renderSearchStreamerCheckboxes);
$('searchStreamerFilter').addEventListener('keydown', event => {
  if (event.key === 'Escape') closeSearchStreamerPicker({returnFocus:true});
});
document.addEventListener('click', event => {
  const picker = document.querySelector('.streamer-picker');
  if (picker && !picker.contains(event.target)) closeSearchStreamerPicker();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('searchStreamerPickerPanel')?.hidden) closeSearchStreamerPicker({returnFocus:true});
});
$('searchStreamersAll').addEventListener('click', () => setAllSearchStreamers(true));
$('searchStreamersNone').addEventListener('click', () => setAllSearchStreamers(false));
$('closeSearchStreamerPicker').addEventListener('click', () => closeSearchStreamerPicker({returnFocus:true}));
$('findVodsTab').addEventListener('click', () => showVodWorkspaceTab('find'));
$('localVodsTab').addEventListener('click', () => showVodWorkspaceTab('local'));
[$('findVodsTab'), $('localVodsTab')].forEach((tab, index, tabs) => tab?.addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next]?.focus();
  showVodWorkspaceTab(next === 0 ? 'find' : 'local');
}));
['includeUnknownDates', 'strictDateFilter', 'excludeLiveStreams', 'onlyRealVodUrls'].forEach(id => $(id).addEventListener('change', updateVodFilterCount));
$('searchBtn').addEventListener('click', () => searchVods().catch(e => alert(e.message)));
$('checkAll').addEventListener('change', e => { document.querySelectorAll('.rowcheck').forEach(cb => cb.checked = e.target.checked); refreshSelectionState(); });
$('selectNewResults').addEventListener('click', () => selectNewResults());
$('clearResultsSelection').addEventListener('click', () => clearResultsSelection());
$('singleDownload').addEventListener('click', () => startSingleVodDownload().catch(e => { setSingleVodStatus('Error: ' + e.message, 'bad'); alert('VOD download failed:\n\n' + e.message); }));
$('streamerAddButton').addEventListener('click', addStreamerFromInput);
$('streamerAddInput').addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addStreamerFromInput();
});
$('streamerListSearch').addEventListener('input', event => {
  setStreamerListDiscovery({searchQuery:event.target.value});
});
$('streamerListSearch').addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !event.currentTarget.value) return;
  event.preventDefault();
  setStreamerListDiscovery({searchQuery:''});
});
document.querySelectorAll('[data-streamer-filter]').forEach(button => {
  button.addEventListener('click', () => {
    setStreamerListDiscovery({filter:button.dataset.streamerFilter});
  });
});
$('saveStreamers').addEventListener('click', () => {
  const button = $('saveStreamers');
  const status = $('streamerListSaveStatus');
  return withButtonPending(button, {pendingLabel:'Saving...'}, async () => {
    if (status) { status.textContent = 'Saving...'; status.className = 'field-message muted'; }
    try {
      const saved = await api('/api/streamers', { method:'POST', body: JSON.stringify({ streamers: $('streamersText').value, streamer_profiles:streamerProfileDraft }) });
      streamerListDirty = false;
      await loadState();
      if ($('streamerFileInfo')) $('streamerFileInfo').textContent = saved.streamer_file || state.streamer_file_resolved || 'unknown';
      if ($('streamerFileStatus')) $('streamerFileStatus').textContent = `${saved.count || 0} streamers saved`;
      refreshAutoRecorderStatus().catch(() => {});
      refreshAutoVodStatus().catch(() => {});
      if (status) { status.textContent = 'Saved.'; status.className = 'field-message good'; }
    } catch (error) {
      streamerListDirty = true;
      if (status) { status.textContent = 'Save failed: ' + (error.message || 'Streamer list could not be saved.'); status.className = 'field-message bad'; }
    }
  }).finally(() => {
    if (button) button.disabled = !streamerListDirty;
  });
});
$('autoRecorderEnabled').addEventListener('change', () => { updateAutoRecorderSettingCopy(); markAutomationSettingsDirty(); });
$('autoVodEnabled').addEventListener('change', () => { updateAutoVodSettingCopy(); markAutomationSettingsDirty(); });
$('autoYoutubeEnabled').addEventListener('change', () => { updateAutoYoutubeSettingCopy(); markAutomationSettingsDirty(); });
$('autoVodPollMinutes').addEventListener('change', markAutomationSettingsDirty);
$('autoYoutubeCleanupDelayHours').addEventListener('change', markAutomationSettingsDirty);
$('saveAutomationSettings').addEventListener('click', saveAutomationSettings);
$('manualDownloadWorkflowMode').addEventListener('change', applyManualDownloadWorkflowChoice);
$('checkAutoVodNow').addEventListener('click', async () => {
  try { await api('/api/auto-vod/check-now', {method:'POST', body:'{}'}); await refreshAutoVodStatus(); }
  catch (e) { showToast(e.message || 'Auto VOD is unavailable.'); }
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
    alert('YouTube connection failed:\n\n' + friendlyYoutubeConnectError(e.message));
  }
});
$('youtubeLoadPlaylists').addEventListener('click', () => {
  withButtonPending($('youtubeLoadPlaylists'), {pendingLabel:'Refreshing...'}, () => loadYoutubePlaylists())
    .then(() => showToast('Playlists loaded.', {variant:'success'}))
    .catch(e => showToast(e.message, {variant:'error'}));
});
$('saveYoutubeSettings').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'youtube'));
$('saveAdvancedSettings').addEventListener('click', (e) => window.vodRobustSaveSettings(e, 'advanced'));
$('refreshLiveStatuses').addEventListener('click', () => {
  withButtonPending($('refreshLiveStatuses'), {pendingLabel:'Refreshing...'}, () => refreshLiveStatuses()).catch(() => {});
});

function markSettingsScopeDirty(scope) {
  const statusId = scope === 'youtube'
    ? 'youtubeSaveStatus'
    : scope === 'advanced'
      ? 'advancedSaveStatus'
      : 'generalSaveStatus';
  const status = $(statusId);
  if (status) { status.textContent = 'Unsaved changes.'; status.className = 'field-message muted'; }
}

document.querySelectorAll('#settingsPanelGeneral input, #settingsPanelGeneral select, #settingsPanelGeneral textarea').forEach(control => {
  control.addEventListener('input', () => markSettingsScopeDirty('general'));
  control.addEventListener('change', () => markSettingsScopeDirty('general'));
});
document.querySelectorAll('#settingsPanelYoutube input, #settingsPanelYoutube select, #settingsPanelYoutube textarea').forEach(control => {
  control.addEventListener('input', () => markSettingsScopeDirty('youtube'));
  control.addEventListener('change', () => markSettingsScopeDirty('youtube'));
});
document.querySelectorAll('#settingsPanelAdvanced input, #settingsPanelAdvanced select, #settingsPanelAdvanced textarea').forEach(control => {
  control.addEventListener('input', () => markSettingsScopeDirty('advanced'));
  control.addEventListener('change', () => markSettingsScopeDirty('advanced'));
});

setInterval(() => pollJobs().catch(() => {}), 5000);
setInterval(() => refreshAutoRecorderStatus().catch(() => {}), AUTO_RECORDER_STATUS_REFRESH_MS);
setInterval(() => refreshAutoVodStatus().catch(() => {}), AUTO_RECORDER_STATUS_REFRESH_MS);
loadState().then(() => {
  setDateRange(7);
  showPage(localStorage.getItem('vodActivePage') || 'dashboard');
  refreshYoutubeStatus();
  refreshDashboard();
  refreshAutoRecorderStatus();
  refreshAutoVodStatus();
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
    if (uploadBtn) uploadBtn.onclick = function(ev) {
      ev.preventDefault();
      withButtonPending(uploadBtn, {pendingLabel:'Adding to Queue...'}, uploadSelectedLocalVideos)
        .catch(e => alert(e.message));
    };
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
    const filter = document.getElementById('localVodsFilter');
    if (filter) filter.onchange = function() { loadLocalVideos().catch(e => alert(e.message)); };
  });
})();

try { window.refreshDashboard = refreshDashboard; } catch(e) {}
try { window.refreshAutoRecorderStatus = refreshAutoRecorderStatus; } catch(e) {}

document.addEventListener('DOMContentLoaded', function(){ const b=document.getElementById('checkSettingsStatus'); if(b) b.onclick=function(e){ window.vodCheckSettingsStatus(e); return false; }; });

document.addEventListener('DOMContentLoaded', function(){ const b=document.getElementById('checkStreamerFile'); if(b) b.onclick=function(e){ e.preventDefault(); checkStreamerFileStatus().catch(err => showToast(err.message, {variant:'error'})); return false; }; });


document.addEventListener('DOMContentLoaded', function() {
  const manualBtn = document.getElementById('resetManualFilenameTemplate');
  if (manualBtn) manualBtn.onclick = function(ev) {
    ev.preventDefault();
    $('manualUploadFilenameTemplate').value = MANUAL_UPLOAD_DEFAULT_FILENAME_TEMPLATE;
    showToast('The final YouTube filename template was reset.', {variant:'info'});
    return false;
  };

  const ytdlpBtn = document.getElementById('resetYtdlpOutputTemplate');
  if (ytdlpBtn) ytdlpBtn.onclick = function(ev) {
    ev.preventDefault();
    $('outputTemplate').value = YTDLP_DEFAULT_OUTPUT_TEMPLATE;
    showToast('The technical yt-dlp output template was reset.', {variant:'info'});
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
  const stickyDownload = document.getElementById('downloadSelected');
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
  const settingsTabs = [...document.querySelectorAll('.settings-tab')];
  settingsTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => showSettingsTab(tab.dataset.settingsTab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? settingsTabs.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + settingsTabs.length) % settingsTabs.length;
      settingsTabs[next].focus();
      showSettingsTab(settingsTabs[next].dataset.settingsTab);
    });
  });
  let initialTab = 'general';
  try { initialTab = localStorage.getItem('vodSettingsTab') || 'general'; } catch {}
  showSettingsTab(initialTab);

  const toggle = document.getElementById('mobileNavToggle');
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (toggle && sidebar) {
    const closeMobileNav = function(returnFocus) {
      sidebar.classList.remove('mobile-open');
      if (backdrop) backdrop.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      toggle.textContent = 'Menu';
      if (returnFocus) toggle.focus();
    };
    const openMobileNav = function() {
      sidebar.classList.add('mobile-open');
      if (backdrop) backdrop.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
      toggle.textContent = 'Close menu';
      const firstNavItem = sidebar.querySelector('.nav-btn');
      if (firstNavItem) firstNavItem.focus();
    };
    toggle.addEventListener('click', () => {
      const open = sidebar.classList.contains('mobile-open');
      if (open) closeMobileNav(true);
      else openMobileNav();
    });
    if (backdrop) backdrop.addEventListener('click', () => closeMobileNav(true));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && sidebar.classList.contains('mobile-open')) {
        event.preventDefault();
        closeMobileNav(true);
      }
    });
    sidebar.querySelectorAll('.nav-btn').forEach(btn => btn.addEventListener('click', () => {
      if (sidebar.classList.contains('mobile-open')) closeMobileNav(false);
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
