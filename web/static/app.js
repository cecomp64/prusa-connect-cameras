'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let cameras = [];       // latest camera list from the API
let editingName = null; // camera name being edited in modal, null when adding
let logWs = null;
let toastTimer = null;

// ── Bootstrap ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Tab nav
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

  // Header
  document.getElementById('restart-btn').addEventListener('click', restartService);

  // Settings forms
  document.getElementById('prusalink-form').addEventListener('submit', e => { e.preventDefault(); savePrusaLink(e.target); });
  document.getElementById('youtube-form').addEventListener('submit', e => { e.preventDefault(); saveYouTube(e.target); });
  document.getElementById('recording-form').addEventListener('submit', e => { e.preventDefault(); saveRecordingConfig(e.target); });

  // Settings — cameras section (event delegation on the list container)
  document.getElementById('add-cam-btn').addEventListener('click', () => openModal(null));
  document.getElementById('cam-list').addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const name = btn.dataset.cam;
    if (btn.dataset.action === 'edit')   openModal(cameras.find(c => c.name === name));
    if (btn.dataset.action === 'delete') deleteCamera(name);
  });

  // Recordings section (event delegation)
  document.getElementById('refresh-recs-btn').addEventListener('click', loadRecordings);
  document.getElementById('rec-list').addEventListener('click', e => {
    const btn = e.target.closest('[data-action="delete-rec"]');
    if (!btn) return;
    deleteRecording(btn.dataset.file);
  });

  // Log controls
  document.getElementById('clear-logs-btn').addEventListener('click', clearLogs);

  // Modal wiring
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-cancel').addEventListener('click', closeModal);
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.getElementById('modal-save').addEventListener('click', saveCamera);
  document.getElementById('gen-fp-btn').addEventListener('click', () => {
    document.querySelector('#camera-form [name="fingerprint"]').value = uuid4();
  });
  document.getElementById('preview-refresh-btn').addEventListener('click', () => {
    if (editingName) loadPreviewImage(editingName);
  });
  document.getElementById('yt-auth-refresh-btn').addEventListener('click', loadYouTubeAuthStatus);
  document.getElementById('yt-auth-start-btn').addEventListener('click', startYouTubeAuth);
  document.getElementById('yt-auth-complete-btn').addEventListener('click', completeYouTubeAuth);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  // Password show/hide (all .toggle-pw buttons)
  document.addEventListener('click', e => {
    if (e.target.classList.contains('toggle-pw')) {
      const row = e.target.closest('.input-row');
      const input = row ? row.querySelector('input') : null;
      if (!input) return;
      input.type = input.type === 'password' ? 'text' : 'password';
      e.target.textContent = input.type === 'password' ? 'Show' : 'Hide';
    }
  });

  // Initial load
  refreshServiceStatus();
  setInterval(refreshServiceStatus, 30_000);
  loadCameraGrid();
});

// ── Tab routing ───────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.id === `tab-${name}`));

  if (name === 'streams')    loadCameraGrid();
  if (name === 'settings')   { loadCameraList(); loadPrusaLink(); loadYouTube(); loadRecordingConfig(); }
  if (name === 'recordings') loadRecordings();
  if (name === 'logs')       startLogStream();
}

// ── Service status bar ────────────────────────────────────────────────────────
async function refreshServiceStatus() {
  const badge = document.getElementById('svc-badge');
  try {
    const { active, state } = await api('/api/service/status');
    badge.className = `badge badge-${active ? 'active' : 'inactive'}`;
    badge.textContent = active ? 'Running' : (state || 'Stopped');
  } catch {
    badge.className = 'badge badge-unknown';
    badge.textContent = 'Unknown';
  }
}

async function restartService() {
  const btn = document.getElementById('restart-btn');
  btn.disabled = true;
  btn.textContent = 'Restarting…';
  try {
    await api('/api/service/restart', { method: 'POST' });
    toast('Service restarting…', 'info');
    setTimeout(() => { refreshServiceStatus(); btn.disabled = false; btn.innerHTML = '&#8635; Restart'; }, 3000);
  } catch (e) {
    toast(`Restart failed: ${e.message}`, 'error');
    btn.disabled = false;
    btn.innerHTML = '&#8635; Restart';
  }
}

// ── Camera streams grid ───────────────────────────────────────────────────────
async function loadCameraGrid() {
  try {
    cameras = await api('/api/cameras');
  } catch {
    cameras = [];
  }

  const grid  = document.getElementById('camera-grid');
  const empty = document.getElementById('no-cameras');

  grid.innerHTML = '';

  if (cameras.length === 0) {
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  cameras.forEach(cam => grid.appendChild(buildCameraCard(cam)));
}

function buildCameraCard(cam) {
  const card = document.createElement('div');
  card.className = 'camera-card';

  const streamContent = cam.webrtc_url
    ? `<iframe src="${esc(cam.webrtc_url)}" frameborder="0" allow="autoplay" allowfullscreen></iframe>`
    : `<div class="stream-no-url">
         <span>&#128247;</span>
         <span>No WebRTC URL set</span>
         <button class="btn btn-ghost btn-sm" data-action="edit">Configure</button>
       </div>`;

  card.innerHTML = `
    <div class="stream-wrap">${streamContent}</div>
    <div class="cam-bar">
      <span class="cam-name">${esc(cam.name)}</span>
      <div class="cam-actions">
        <button class="btn btn-ghost btn-sm" data-action="edit">Edit</button>
      </div>
    </div>
  `;

  card.addEventListener('click', e => {
    if (e.target.closest('[data-action="edit"]')) openModal(cam);
  });

  return card;
}

// ── Camera list (settings) ────────────────────────────────────────────────────
async function loadCameraList() {
  try {
    cameras = await api('/api/cameras');
  } catch {
    cameras = [];
  }

  const list = document.getElementById('cam-list');

  if (cameras.length === 0) {
    list.innerHTML = '<div class="cam-list-empty">No cameras configured.</div>';
    return;
  }

  list.innerHTML = cameras.map(cam => `
    <div class="cam-item">
      <div class="cam-item-info">
        <div class="cam-item-name">${esc(cam.name)}</div>
        <div class="cam-item-url">${esc(cam.rtsp_url)}</div>
      </div>
      <div class="cam-item-actions">
        <button class="btn btn-ghost btn-sm"              data-action="edit"   data-cam="${esc(cam.name)}">Edit</button>
        <button class="btn btn-ghost btn-sm btn-danger"   data-action="delete" data-cam="${esc(cam.name)}">Delete</button>
      </div>
    </div>
  `).join('');
}

// ── Camera modal ──────────────────────────────────────────────────────────────
function openModal(cam) {
  editingName = cam ? cam.name : null;
  const form    = document.getElementById('camera-form');
  const title   = document.getElementById('modal-title');
  const preview = document.getElementById('cam-preview');

  title.textContent = cam ? 'Edit Camera' : 'Add Camera';
  form.reset();

  if (cam) {
    form.elements.name.value              = cam.name;
    form.elements.webrtc_url.value        = cam.webrtc_url || '';
    form.elements.rtsp_url.value          = cam.rtsp_url;
    form.elements.token.value             = cam.token || '';
    form.elements.fingerprint.value       = cam.fingerprint || '';
    form.elements.snapshot_interval.value = cam.snapshot_interval ?? 10;
    preview.classList.remove('hidden');
    loadPreviewImage(cam.name);
  } else {
    preview.classList.add('hidden');
  }

  document.getElementById('modal-overlay').classList.remove('hidden');
  form.elements.name.focus();
}

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  editingName = null;
}

function loadPreviewImage(name) {
  const img = document.getElementById('preview-img');
  const err = document.getElementById('preview-err');
  err.classList.add('hidden');
  img.style.display = '';
  img.src = `/api/stream/${encodeURIComponent(name)}/snapshot?t=${Date.now()}`;
  img.onerror = () => { img.style.display = 'none'; err.classList.remove('hidden'); };
  img.onload  = () => { err.classList.add('hidden'); };
}

async function saveCamera() {
  const form = document.getElementById('camera-form');
  const body = {
    name:              form.elements.name.value.trim(),
    webrtc_url:        form.elements.webrtc_url.value.trim(),
    rtsp_url:          form.elements.rtsp_url.value.trim(),
    token:             form.elements.token.value.trim(),
    fingerprint:       form.elements.fingerprint.value.trim(),
    snapshot_interval: parseInt(form.elements.snapshot_interval.value) || 10,
  };

  if (!body.name || !body.rtsp_url || !body.token) {
    toast('Name, RTSP URL, and token are required', 'error');
    return;
  }

  const saveBtn = document.getElementById('modal-save');
  saveBtn.disabled = true;

  try {
    if (editingName) {
      await api(`/api/cameras/${encodeURIComponent(editingName)}`, { method: 'PUT', json: body });
      toast('Camera updated — restart service to apply', 'success');
    } else {
      await api('/api/cameras', { method: 'POST', json: body });
      toast('Camera added — restart service to apply', 'success');
    }
    closeModal();
    loadCameraList();
    loadCameraGrid();
  } catch (e) {
    toast(`Save failed: ${e.message}`, 'error');
  } finally {
    saveBtn.disabled = false;
  }
}

async function deleteCamera(name) {
  if (!confirm(`Delete camera "${name}"?\nThis cannot be undone.`)) return;
  try {
    await api(`/api/cameras/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('Camera deleted — restart service to apply', 'success');
    loadCameraList();
    loadCameraGrid();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, 'error');
  }
}

// ── Settings forms ────────────────────────────────────────────────────────────
async function loadPrusaLink() {
  try {
    const d = await api('/api/prusalink');
    const f = document.getElementById('prusalink-form');
    f.elements.host.value         = d.host || '';
    f.elements.api_key.value      = d.api_key || '';
    f.elements.poll_interval.value = d.poll_interval ?? 15;
  } catch {}
}

async function savePrusaLink(form) {
  try {
    await api('/api/prusalink', {
      method: 'PUT',
      json: {
        host:          form.elements.host.value.trim(),
        api_key:       form.elements.api_key.value.trim(),
        poll_interval: parseInt(form.elements.poll_interval.value) || 15,
      },
    });
    toast('PrusaLink saved — restart service to apply', 'success');
  } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}

async function loadYouTube() {
  try {
    const d = await api('/api/youtube');
    const f = document.getElementById('youtube-form');
    f.elements.enabled.checked           = !!d.enabled;
    f.elements.privacy.value             = d.privacy || 'unlisted';
    f.elements.client_secrets_file.value = d.client_secrets_file || '';
    f.elements.credentials_cache.value   = d.credentials_cache || '';
    f.elements.playlist_id.value         = d.playlist_id || '';
  } catch {}
  loadYouTubeAuthStatus();
}

async function loadYouTubeAuthStatus() {
  const badge = document.getElementById('yt-auth-badge');
  try {
    const { authorized } = await api('/api/youtube/auth/status');
    badge.className = `badge badge-${authorized ? 'active' : 'inactive'}`;
    badge.textContent = authorized ? 'Authorized' : 'Not authorized';
  } catch {
    badge.className = 'badge badge-unknown';
    badge.textContent = 'Unknown';
  }
}

async function startYouTubeAuth() {
  const btn = document.getElementById('yt-auth-start-btn');
  btn.disabled = true;
  try {
    const { auth_url } = await api('/api/youtube/auth/start', { method: 'POST' });
    document.getElementById('yt-auth-link').href = auth_url;
    document.getElementById('yt-redirect-paste').value = '';
    document.getElementById('yt-auth-step2').classList.remove('hidden');
    // Open in new tab automatically
    window.open(auth_url, '_blank');
  } catch (e) {
    toast(`Could not start auth: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function completeYouTubeAuth() {
  const redirectUrl = document.getElementById('yt-redirect-paste').value.trim();
  if (!redirectUrl) {
    toast('Paste the redirect URL first', 'error');
    return;
  }
  const btn = document.getElementById('yt-auth-complete-btn');
  btn.disabled = true;
  try {
    await api('/api/youtube/auth/complete', { method: 'POST', json: { redirect_url: redirectUrl } });
    toast('YouTube authorized!', 'success');
    document.getElementById('yt-auth-step2').classList.add('hidden');
    loadYouTubeAuthStatus();
  } catch (e) {
    toast(`Authorization failed: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function saveYouTube(form) {
  try {
    await api('/api/youtube', {
      method: 'PUT',
      json: {
        enabled:             form.elements.enabled.checked,
        privacy:             form.elements.privacy.value,
        client_secrets_file: form.elements.client_secrets_file.value.trim(),
        credentials_cache:   form.elements.credentials_cache.value.trim(),
        playlist_id:         form.elements.playlist_id.value.trim(),
      },
    });
    toast('YouTube settings saved', 'success');
  } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}

async function loadRecordingConfig() {
  try {
    const d = await api('/api/recording-config');
    const f = document.getElementById('recording-form');
    f.elements.output_dir.value     = d.output_dir || '';
    f.elements.retention_days.value = d.retention_days ?? 7;
  } catch {}
}

async function saveRecordingConfig(form) {
  try {
    await api('/api/recording-config', {
      method: 'PUT',
      json: {
        output_dir:     form.elements.output_dir.value.trim(),
        retention_days: parseInt(form.elements.retention_days.value) || 0,
      },
    });
    toast('Recording config saved', 'success');
  } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}

// ── Recordings tab ────────────────────────────────────────────────────────────
async function loadRecordings() {
  const list  = document.getElementById('rec-list');
  const empty = document.getElementById('no-recs');
  list.innerHTML = '';

  try {
    const recs = await api('/api/recordings');

    if (recs.length === 0) {
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');

    list.innerHTML = recs.map(r => `
      <div class="rec-item">
        <div class="rec-info">
          <div class="rec-name">${esc(r.name)}</div>
          <div class="rec-meta">${fmtBytes(r.size)} &middot; ${fmtDate(r.mtime)}</div>
        </div>
        <button class="btn btn-ghost btn-sm btn-danger" data-action="delete-rec" data-file="${esc(r.name)}">Delete</button>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<p style="color:var(--red);padding:8px">Error: ${esc(e.message)}</p>`;
  }
}

async function deleteRecording(filename) {
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    await api(`/api/recordings/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    toast('Recording deleted', 'success');
    loadRecordings();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, 'error');
  }
}

// ── Live log WebSocket ────────────────────────────────────────────────────────
function startLogStream() {
  if (logWs && logWs.readyState === WebSocket.OPEN) return;

  const output = document.getElementById('log-output');
  const badge  = document.getElementById('log-badge');
  const proto  = location.protocol === 'https:' ? 'wss' : 'ws';

  logWs = new WebSocket(`${proto}://${location.host}/ws/logs`);

  badge.className = 'badge badge-unknown';
  badge.textContent = 'Connecting…';

  logWs.onopen = () => {
    badge.className = 'badge badge-active';
    badge.textContent = 'Connected';
  };

  logWs.onmessage = ({ data }) => {
    const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 80;
    output.textContent += data + '\n';
    // cap at 2 000 lines to prevent memory growth
    const lines = output.textContent.split('\n');
    if (lines.length > 2000) output.textContent = lines.slice(-2000).join('\n');
    if (atBottom) output.scrollTop = output.scrollHeight;
  };

  logWs.onclose = () => {
    badge.className = 'badge badge-inactive';
    badge.textContent = 'Disconnected';
    // Reconnect only if still on the logs tab
    setTimeout(() => {
      if (document.querySelector('.tab-btn[data-tab="logs"]').classList.contains('active')) {
        startLogStream();
      }
    }, 3000);
  };

  logWs.onerror = () => {
    badge.className = 'badge badge-inactive';
    badge.textContent = 'Error';
  };
}

function clearLogs() {
  document.getElementById('log-output').textContent = '';
}

// ── Utilities ─────────────────────────────────────────────────────────────────
async function api(url, opts = {}) {
  const init = { method: opts.method || 'GET' };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { 'Content-Type': 'application/json' };
  }
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const err = await resp.json();
      msg = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
    } catch {
      msg = await resp.text().catch(() => resp.statusText);
    }
    throw new Error(msg);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtBytes(n) {
  if (n < 1_024)         return `${n} B`;
  if (n < 1_048_576)     return `${(n / 1_024).toFixed(1)} KB`;
  if (n < 1_073_741_824) return `${(n / 1_048_576).toFixed(1)} MB`;
  return `${(n / 1_073_741_824).toFixed(2)} GB`;
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function uuid4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.className = `toast ${type}`;
  el.textContent = msg;
  void el.offsetWidth; // force reflow so transition fires if already visible
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}
