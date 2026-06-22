'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let cameras = [];       // latest camera list from the API
let editingName = null; // camera name being edited in modal, null when adding
let logWs = null;
let toastTimer = null;
let printerPollTimer = null;
let lastPrinterData  = null;
let systemPollTimer  = null;
let _chartMonthly    = null;
let _chartWeekday    = null;
let _chartDuration   = null;
let _chartOutcome    = null;
let _chartMaterial   = null;
let _chartCpuTemp    = null;
let _chartCpuUsage   = null;
let _chartMemUsage   = null;
let recordingPollTimer = null;
let recordingCameras = new Set(); // names of cameras currently recording
let recordingsRefreshTimer = null;
let statsRefreshTimer = null;
let lastRecordingsKey = null;
let printerConfirmPending = null; // { label, fn } while awaiting inline confirmation
let printerFilesOpen = false;
let _printsPage = 1;
let _printsDebounceTimer = null;

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
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    if (btn.dataset.action === 'delete-rec') deleteRecording(btn.dataset.file);
    if (btn.dataset.action === 'stop-rec')   stopLiveRecording(btn.dataset.cam);
    if (btn.dataset.action === 'upload-rec') uploadRecording(btn.dataset.file);
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
  document.getElementById('upload-close-btn').addEventListener('click', closeUploadModal);
  document.getElementById('upload-cancel-btn').addEventListener('click', closeUploadModal);
  document.getElementById('upload-confirm-btn').addEventListener('click', doUpload);
  document.getElementById('upload-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeUploadModal();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); closeUploadModal(); closePrinterFiles(); closeFilePreview(); closePrintDetail(); } });
  document.getElementById('file-preview-close').addEventListener('click', closeFilePreview);
  document.getElementById('file-preview-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeFilePreview();
  });

  // Print detail modal
  document.getElementById('print-detail-close').addEventListener('click', closePrintDetail);
  document.getElementById('print-detail-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closePrintDetail();
  });
  document.getElementById('pd-reprint-btn').addEventListener('click', showReprintConfirm);
  document.getElementById('pd-reprint-yes').addEventListener('click', doReprint);
  document.getElementById('pd-reprint-cancel').addEventListener('click', hideReprintConfirm);

  // Prints tab
  document.getElementById('prints-list').addEventListener('click', e => {
    const item = e.target.closest('.stats-recent-item');
    if (item && item.dataset.id) openPrintDetail(item.dataset.id);
  });
  document.getElementById('pf-search').addEventListener('input', () => { _printsPage = 1; loadPrintsDebounced(); });
  document.getElementById('pf-material').addEventListener('change', () => { _printsPage = 1; loadPrints(); });
  document.getElementById('pf-min-dur').addEventListener('input', () => { _printsPage = 1; loadPrintsDebounced(); });
  document.getElementById('pf-max-dur').addEventListener('input', () => { _printsPage = 1; loadPrintsDebounced(); });
  document.getElementById('pf-date-from').addEventListener('change', () => { _printsPage = 1; loadPrints(); });
  document.getElementById('pf-date-to').addEventListener('change', () => { _printsPage = 1; loadPrints(); });
  document.getElementById('pf-reset').addEventListener('click', resetPrintsFilters);
  document.getElementById('prints-prev').addEventListener('click', () => { if (_printsPage > 1) { _printsPage--; loadPrints(); } });
  document.getElementById('prints-next').addEventListener('click', () => { _printsPage++; loadPrints(); });

  // Printer file browser
  document.getElementById('printer-files-refresh').addEventListener('click', () => {
    loadPrinterFiles('usb', true);
  });
  document.getElementById('printer-files-close').addEventListener('click', closePrinterFiles);
  document.getElementById('printer-files-list').addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    if (btn.dataset.action === 'print-file')   printFile(btn.dataset.storage, btn.dataset.path, btn.dataset.name);
    if (btn.dataset.action === 'preview-file') openFilePreview(btn.dataset);
  });

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
  loadDashboard();
  startPrinterPoll();
  startRecordingPoll();
  startSystemPoll();
});

// ── Tab routing ───────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.id === `tab-${name}`));

  if (name === 'dashboard') { loadDashboard(); startPrinterPoll(); startRecordingPoll(); startSystemPoll(); }
  else                        { stopPrinterPoll(); stopRecordingPoll(); stopSystemPoll(); }
  if (name === 'settings')   { loadCameraList(); loadPrusaLink(); loadYouTube(); loadRecordingConfig(); }
  if (name === 'recordings') { loadRecordings(); startRecordingsRefresh(); }
  else                         stopRecordingsRefresh();
  if (name === 'prints')     loadPrints();
  if (name === 'stats')      { loadStats(); startStatsRefresh(); }
  else                         stopStatsRefresh();
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
  card.className = `camera-card${cam.orientation === 'portrait' ? ' camera-card--portrait' : ''}`;
  card.dataset.cam = cam.name;

  const isRecording = recordingCameras.has(cam.name);

  const streamContent = cam.webrtc_url
    ? `<iframe src="${esc(cam.webrtc_url)}" frameborder="0" allow="autoplay" allowfullscreen></iframe>`
    : `<div class="stream-no-url">
         <span>&#128247;</span>
         <span>No WebRTC URL set — configure in Settings</span>
       </div>`;

  card.innerHTML = `
    <div class="stream-wrap">${streamContent}</div>
    <div class="cam-bar">
      <span class="cam-name">
        <span class="rec-dot${isRecording ? '' : ' hidden'}" title="Recording"></span>${esc(cam.name)}
      </span>
      <div class="cam-actions">
        <button class="btn btn-ghost btn-sm btn-rec${isRecording ? ' hidden' : ''}" data-action="start-rec">&#9679; Record</button>
      </div>
    </div>
  `;

  card.addEventListener('click', e => {
    if (e.target.closest('[data-action="start-rec"]')) startManualRecording(cam.name);
  });

  return card;
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function loadDashboard() {
  loadCameraGrid();
  await loadPrinterStatus();
  loadSystemStatus();
}

function startRecordingPoll() {
  stopRecordingPoll();
  refreshRecordingStatus();
  recordingPollTimer = setInterval(refreshRecordingStatus, 5_000);
}

function stopRecordingPoll() {
  if (recordingPollTimer !== null) { clearInterval(recordingPollTimer); recordingPollTimer = null; }
}

async function refreshRecordingStatus() {
  try {
    const { recording } = await api('/api/recording-status');
    recordingCameras = new Set(recording.map(s => (typeof s === 'string' ? s : s.name)));
  } catch {
    recordingCameras = new Set();
  }
  // Update dots and record buttons on any already-rendered camera cards
  document.querySelectorAll('.camera-card[data-cam]').forEach(card => {
    const dot = card.querySelector('.rec-dot');
    const recBtn = card.querySelector('.btn-rec');
    const isRecording = recordingCameras.has(card.dataset.cam);
    if (dot) dot.classList.toggle('hidden', !isRecording);
    if (!dot && isRecording) {
      const nameEl = card.querySelector('.cam-name');
      if (nameEl) nameEl.insertAdjacentHTML('afterbegin', '<span class="rec-dot"></span>');
    }
    if (recBtn) recBtn.classList.toggle('hidden', isRecording);
  });
}

function startPrinterPoll() {
  stopPrinterPoll();
  printerPollTimer = setInterval(loadPrinterStatus, 10_000);
}

function stopPrinterPoll() {
  if (printerPollTimer !== null) { clearInterval(printerPollTimer); printerPollTimer = null; }
}

function startSystemPoll() {
  stopSystemPoll();
  systemPollTimer = setInterval(loadSystemStatus, 30_000);
}
function stopSystemPoll() {
  if (systemPollTimer !== null) { clearInterval(systemPollTimer); systemPollTimer = null; }
}

async function loadSystemStatus() {
  let data;
  try { data = await api('/api/system/status'); } catch { return; }

  const fmtTemp = v => v != null ? `${v}°C` : '—';
  const fmtPct  = v => v != null ? `${v.toFixed(0)}%` : '—';
  const fmtMB   = v => v != null ? `${v} MB` : '—';
  const fmtGB   = v => v != null ? `${v} GB` : '—';

  document.getElementById('sys-cpu-temp').textContent  = fmtTemp(data.cpu_temp);
  document.getElementById('sys-cpu-usage').textContent = fmtPct(data.cpu_usage);
  document.getElementById('sys-mem-used').textContent  = fmtMB(data.mem_used);
  document.getElementById('sys-mem-total').textContent = data.mem_total != null ? `/ ${data.mem_total} MB` : '';
  document.getElementById('sys-disk-free').textContent = fmtGB(data.disk_free);
  document.getElementById('sys-disk-total').textContent = data.disk_total != null ? `/ ${data.disk_total} GB` : '';
  document.getElementById('sys-uptime').textContent    = data.uptime != null ? fmtDuration(data.uptime) : '—';

  // Raspberry Pi throttle / under-voltage alerts
  const alertsEl = document.getElementById('sys-alerts');
  const alerts = [];
  if (data.under_voltage)   alerts.push({ cls: 'sys-alert--error', text: 'Under Voltage' });
  if (data.throttled)       alerts.push({ cls: 'sys-alert--error', text: 'CPU Throttling' });
  if (data.soft_temp_limit) alerts.push({ cls: 'sys-alert--warn',  text: 'Soft Temp Limit' });
  if (!data.under_voltage && data.under_voltage_occurred)
    alerts.push({ cls: 'sys-alert--warn', text: 'Under Voltage (since boot)' });
  if (!data.throttled && data.throttled_occurred)
    alerts.push({ cls: 'sys-alert--warn', text: 'Throttled (since boot)' });

  if (alerts.length > 0) {
    alertsEl.innerHTML = alerts.map(a => `<span class="sys-alert ${a.cls}">${a.text}</span>`).join('');
    alertsEl.classList.remove('hidden');
  } else {
    alertsEl.innerHTML = '';
    alertsEl.classList.add('hidden');
  }
}

// ── Stats tab ─────────────────────────────────────────────────────────────────

function _cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function _makeChart(id, cfg) {
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  cfg.options = cfg.options || {};
  cfg.options.animation = false;
  return new Chart(canvas, cfg);
}

function _chartScaleDefaults() {
  const mutedColor  = 'rgba(125,133,144,0.5)';
  const gridColor   = 'rgba(48,54,61,0.8)';
  return {
    x: { grid: { color: gridColor }, ticks: { color: mutedColor } },
    y: { grid: { color: gridColor }, ticks: { color: mutedColor }, beginAtZero: true },
  };
}

async function loadStats() {
  let data;
  try { data = await api('/api/stats'); } catch { return; }

  const fmtH = h => h != null ? `${h}h` : '—';

  document.getElementById('st-total').textContent   = data.total_prints ?? '—';
  document.getElementById('st-hours').textContent   = fmtH(data.total_hours);
  document.getElementById('st-avg').textContent     = fmtH(data.avg_duration_hours);

  const lp = data.longest_print;
  document.getElementById('st-longest').textContent      = lp ? fmtDuration(lp.duration_seconds) : '—';
  document.getElementById('st-longest-name').textContent = lp?.display_name ?? '';

  const accent = '#fa6831';
  const green  = '#3fb950';
  const blue   = '#58a6ff';
  const scales = _chartScaleDefaults();

  // Monthly chart
  if (_chartMonthly) _chartMonthly.destroy();
  _chartMonthly = _makeChart('chart-monthly', {
    type: 'bar',
    data: {
      labels:   data.by_month.map(m => m.label),
      datasets: [{ data: data.by_month.map(m => m.count), backgroundColor: accent, borderRadius: 3 }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales,
    },
  });

  // Weekday chart (hours)
  if (_chartWeekday) _chartWeekday.destroy();
  _chartWeekday = _makeChart('chart-weekday', {
    type: 'bar',
    data: {
      labels:   data.by_weekday.map(d => d.day),
      datasets: [{ data: data.by_weekday.map(d => d.hours), backgroundColor: green, borderRadius: 3 }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales,
    },
  });

  // Duration distribution — horizontal bar
  if (_chartDuration) _chartDuration.destroy();
  _chartDuration = _makeChart('chart-duration', {
    type: 'bar',
    data: {
      labels:   data.by_duration.map(b => b.label),
      datasets: [{ data: data.by_duration.map(b => b.count), backgroundColor: blue, borderRadius: 3 }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ..._chartScaleDefaults().x, ticks: { ..._chartScaleDefaults().x.ticks, stepSize: 1 } },
        y: _chartScaleDefaults().y,
      },
    },
  });

  // Outcome doughnut
  if (_chartOutcome) _chartOutcome.destroy();
  if (data.by_outcome && data.by_outcome.length > 0) {
    const outcomeColors = { FINISHED: green, STOPPED: '#e3b341', ERROR: '#f85149', UNKNOWN: '#6e7681' };
    _chartOutcome = _makeChart('chart-outcome', {
      type: 'doughnut',
      data: {
        labels:   data.by_outcome.map(o => o.state.charAt(0) + o.state.slice(1).toLowerCase()),
        datasets: [{ data: data.by_outcome.map(o => o.count), backgroundColor: data.by_outcome.map(o => outcomeColors[o.state] ?? '#6e7681'), borderWidth: 0 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        cutout: '65%',
        plugins: {
          legend: { display: true, position: 'bottom', labels: { color: _cssVar('--text-muted'), boxWidth: 10, padding: 10, font: { size: 11 } } },
        },
      },
    });
  }

  // Material doughnut
  if (_chartMaterial) _chartMaterial.destroy();
  const knownMaterials = (data.by_material || []).filter(m => m.material !== 'Unknown');
  if (knownMaterials.length > 0) {
    const materialPalette = ['#58a6ff', '#3fb950', '#e3b341', '#f85149', '#a371f7', '#fa6831', '#79c0ff', '#56d364'];
    _chartMaterial = _makeChart('chart-material', {
      type: 'doughnut',
      data: {
        labels:   knownMaterials.map(m => m.material),
        datasets: [{ data: knownMaterials.map(m => m.count), backgroundColor: knownMaterials.map((_, i) => materialPalette[i % materialPalette.length]), borderWidth: 0 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        cutout: '65%',
        plugins: {
          legend: { display: true, position: 'bottom', labels: { color: _cssVar('--text-muted'), boxWidth: 10, padding: 10, font: { size: 11 } } },
        },
      },
    });
  }

  loadSystemStats();
}

function buildStatsRecentItem(p) {
  const name     = esc(p.display_name ?? '(unknown)');
  const dur      = p.duration_seconds != null ? fmtDuration(p.duration_seconds) : '—';
  const inProg   = !p.end_time;
  const stateRaw = inProg ? 'PRINTING' : (p.end_state || 'UNKNOWN').toUpperCase();
  const badgeCls = printerStateBadgeClass(stateRaw);
  const dateStr  = p.start_time ? new Date(p.start_time).toLocaleDateString() : '—';
  return `<div class="stats-recent-item" data-id="${esc(p.id)}">
    <span class="stats-recent-name">${name}</span>
    <span class="stats-recent-date">${dateStr}</span>
    <span class="stats-recent-dur">${dur}</span>
    <span class="printer-badge ${badgeCls}">${stateRaw}</span>
  </div>`;
}

// ── Prints tab ────────────────────────────────────────────────────────────────

function loadPrintsDebounced() {
  clearTimeout(_printsDebounceTimer);
  _printsDebounceTimer = setTimeout(loadPrints, 380);
}

async function loadPrints() {
  const search   = document.getElementById('pf-search').value.trim();
  const material = document.getElementById('pf-material').value;
  const minDur   = document.getElementById('pf-min-dur').value;
  const maxDur   = document.getElementById('pf-max-dur').value;
  const dateFrom = document.getElementById('pf-date-from').value;
  const dateTo   = document.getElementById('pf-date-to').value;

  const params = new URLSearchParams({ page: _printsPage, per_page: 25 });
  if (search)   params.set('search', search);
  if (material) params.set('material', material);
  if (minDur)   params.set('min_duration', Math.round(parseFloat(minDur) * 3600));
  if (maxDur)   params.set('max_duration', Math.round(parseFloat(maxDur) * 3600));
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo)   params.set('date_to', dateTo);

  let data;
  try { data = await api(`/api/prints?${params}`); } catch { return; }

  // Refresh material dropdown, preserving current selection
  const sel = document.getElementById('pf-material');
  const selectedMat = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  (data.materials || []).forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  sel.value = selectedMat;

  const list  = document.getElementById('prints-list');
  const empty = document.getElementById('prints-no-data');
  const pag   = document.getElementById('prints-pagination');

  if (!data.prints || data.prints.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    pag.classList.add('hidden');
    return;
  }

  empty.classList.add('hidden');
  list.innerHTML = data.prints.map(buildStatsRecentItem).join('');

  const total = data.total;
  const per   = data.per_page;
  const page  = data.page;
  const start = (page - 1) * per + 1;
  const end   = Math.min(page * per, total);

  document.getElementById('prints-page-info').textContent = `${start}–${end} of ${total}`;
  document.getElementById('prints-prev').disabled = page <= 1;
  document.getElementById('prints-next').disabled = end >= total;
  pag.classList.toggle('hidden', total <= per);
}

function resetPrintsFilters() {
  document.getElementById('pf-search').value = '';
  document.getElementById('pf-material').value = '';
  document.getElementById('pf-min-dur').value = '';
  document.getElementById('pf-max-dur').value = '';
  document.getElementById('pf-date-from').value = '';
  document.getElementById('pf-date-to').value = '';
  _printsPage = 1;
  loadPrints();
}

// ── Print detail modal ────────────────────────────────────────────────────────

let _printDetailId = null;

async function openPrintDetail(id) {
  _printDetailId = id;
  hideReprintConfirm();
  const overlay = document.getElementById('print-detail-overlay');
  overlay.classList.remove('hidden');

  // Reset state
  document.getElementById('print-detail-title').textContent = 'Loading…';
  document.getElementById('pd-start').textContent        = '—';
  document.getElementById('pd-end').textContent          = '—';
  document.getElementById('pd-duration').textContent     = '—';
  document.getElementById('pd-state').innerHTML          = '';
  document.getElementById('pd-recordings-list').innerHTML = '';
  document.getElementById('pd-events-list').innerHTML    = '';
  document.getElementById('pd-notes').value              = '';
  document.getElementById('pd-notes-status').textContent = '';
  document.getElementById('pd-no-recordings').classList.add('hidden');
  document.getElementById('pd-no-events').classList.add('hidden');

  let data;
  try {
    data = await api(`/api/print/${encodeURIComponent(id)}`);
  } catch (err) {
    document.getElementById('print-detail-title').textContent = 'Error loading print';
    return;
  }

  const title = data.display_name ?? '(unknown)';
  document.getElementById('print-detail-title').textContent = title;

  document.getElementById('pd-start').textContent =
    data.start_time ? new Date(data.start_time).toLocaleString() : '—';
  document.getElementById('pd-end').textContent =
    data.end_time ? new Date(data.end_time).toLocaleString() : '—';
  document.getElementById('pd-duration').textContent =
    data.duration_seconds != null ? fmtDuration(data.duration_seconds) : '—';

  const stateRaw = (data.end_state || 'UNKNOWN').toUpperCase();
  const badgeCls = printerStateBadgeClass(stateRaw);
  document.getElementById('pd-state').innerHTML =
    `<span class="printer-badge ${badgeCls}">${esc(stateRaw)}</span>`;

  // Recordings
  const recList = document.getElementById('pd-recordings-list');
  if (!data.recordings || data.recordings.length === 0) {
    document.getElementById('pd-no-recordings').classList.remove('hidden');
  } else {
    recList.innerHTML = data.recordings.map(r => {
      const dur      = r.duration_seconds != null ? fmtDuration(r.duration_seconds) : null;
      const size     = r.file_size_mb != null ? `${r.file_size_mb} MB` : null;
      const meta     = [dur, size].filter(Boolean).join(' · ');
      const filename = r.file_path ? r.file_path.split('/').pop() : '';

      let ytHtml = '';
      if (r.yt_url) {
        ytHtml = `<a href="${esc(r.yt_url)}" target="_blank" rel="noopener" class="btn btn-ghost btn-sm yt-done-btn">&#9654; YouTube</a>`;
      } else if (r.yt_status === 'uploading' || r.yt_status === 'pending') {
        ytHtml = `<span class="badge badge-uploading">Uploading…</span>`;
      }

      const deletedTag = r.file_deleted ? `<span class="pd-recording-deleted">File deleted</span>` : '';

      return `<div class="pd-recording-item">
        <div class="pd-recording-cam">${esc(r.camera)}</div>
        <div class="pd-recording-meta">${esc(filename)}${meta ? ' · ' + esc(meta) : ''}${deletedTag ? ' · ' + deletedTag : ''}</div>
        ${ytHtml}
      </div>`;
    }).join('');
  }

  // Events
  const evList = document.getElementById('pd-events-list');
  if (!data.events || data.events.length === 0) {
    document.getElementById('pd-no-events').classList.remove('hidden');
  } else {
    const dotClass = type => {
      if (type === 'print_start')      return 'stats-event-dot--print-start';
      if (type === 'print_end')        return 'stats-event-dot--print-end';
      if (type === 'recording_start')  return 'stats-event-dot--upload-done';
      if (type === 'recording_stop')   return 'stats-event-dot--print-end';
      if (type === 'printer_message')  return 'stats-event-dot--printer-message';
      return 'stats-event-dot--print-end';
    };
    evList.innerHTML = data.events.map(e => {
      const timeStr = e.time
        ? new Date(e.time).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '—';
      return `<div class="pd-event-item">
        <span class="pd-event-dot ${dotClass(e.type)}"></span>
        <div class="pd-event-content">
          <span class="pd-event-label">${esc(e.label)}</span>
          <span class="pd-event-time">${esc(timeStr)}</span>
        </div>
      </div>`;
    }).join('');
  }

  // Notes
  document.getElementById('pd-notes').value = data.notes ?? '';
  document.getElementById('pd-notes-save').onclick = () => savePrintNotes(id);
}

function closePrintDetail() {
  document.getElementById('print-detail-overlay').classList.add('hidden');
  _printDetailId = null;
}

async function savePrintNotes(id) {
  const notes  = document.getElementById('pd-notes').value;
  const status = document.getElementById('pd-notes-status');
  status.textContent = 'Saving…';
  try {
    await api(`/api/print/${encodeURIComponent(id)}/notes`, { method: 'PUT', json: { notes } });
    status.textContent = 'Saved.';
    setTimeout(() => { if (status.textContent === 'Saved.') status.textContent = ''; }, 2000);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
}

function showReprintConfirm() {
  document.getElementById('pd-reprint-btn').classList.add('hidden');
  document.getElementById('pd-reprint-confirm').classList.remove('hidden');
}

function hideReprintConfirm() {
  document.getElementById('pd-reprint-confirm').classList.add('hidden');
  document.getElementById('pd-reprint-btn').classList.remove('hidden');
}

async function doReprint() {
  if (!_printDetailId) return;
  hideReprintConfirm();
  try {
    const result = await api(`/api/print/${encodeURIComponent(_printDetailId)}/reprint`, { method: 'POST' });
    toast(`Re-print started: ${result.file}`, 'success');
    closePrintDetail();
    loadPrinterStatus();
  } catch (e) {
    toast(`Re-print failed: ${e.message}`, 'error');
  }
}

async function loadSystemStats() {
  let data;
  try { data = await api('/api/stats/system'); } catch { return; }

  const orange = '#fa6831';
  const blue   = '#58a6ff';
  const green  = '#3fb950';

  function fmtBucketLabel(ts) {
    const d = new Date(ts * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  const labels    = data.metrics.map(m => fmtBucketLabel(m.ts));
  const xScale    = {
    ..._chartScaleDefaults().x,
    ticks: { ..._chartScaleDefaults().x.ticks, maxTicksLimit: 8, maxRotation: 0 },
  };

  function lineDataset(color, key) {
    return {
      data: data.metrics.map(m => m[key]),
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
      fill: true,
    };
  }

  function lineOpts(unit) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: xScale,
        y: {
          ..._chartScaleDefaults().y,
          ticks: {
            ..._chartScaleDefaults().y.ticks,
            callback: v => v != null ? v + unit : '',
          },
        },
      },
    };
  }

  const hasCpuTemp = data.metrics.some(m => m.cpu_temp != null);

  if (hasCpuTemp) {
    if (_chartCpuTemp) _chartCpuTemp.destroy();
    _chartCpuTemp = _makeChart('chart-cpu-temp', {
      type: 'line',
      data: { labels, datasets: [lineDataset(orange, 'cpu_temp')] },
      options: lineOpts('°C'),
    });
  }

  if (_chartCpuUsage) _chartCpuUsage.destroy();
  _chartCpuUsage = _makeChart('chart-cpu-usage', {
    type: 'line',
    data: { labels, datasets: [lineDataset(blue, 'cpu_usage')] },
    options: lineOpts('%'),
  });

  if (_chartMemUsage) _chartMemUsage.destroy();
  _chartMemUsage = _makeChart('chart-mem-usage', {
    type: 'line',
    data: { labels, datasets: [lineDataset(green, 'mem_pct')] },
    options: lineOpts('%'),
  });

  // Events list
  const evList  = document.getElementById('stats-events-list');
  const evEmpty = document.getElementById('stats-no-events');
  if (!data.events || data.events.length === 0) {
    evList.innerHTML = '';
    evEmpty.classList.remove('hidden');
  } else {
    evEmpty.classList.add('hidden');
    evList.innerHTML = data.events.map(buildEventItem).join('');
  }
}

function buildEventItem(e) {
  const timeStr = e.time
    ? new Date(e.time).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : '—';
  let dotCls = 'stats-event-dot--print-end';
  if (e.type === 'print_start') {
    dotCls = 'stats-event-dot--print-start';
  } else if (e.type === 'print_end') {
    const s = (e.state || '').toUpperCase();
    if (s === 'FINISHED') dotCls = 'stats-event-dot--print-finished';
    else if (s === 'ERROR' || s === 'STOPPED') dotCls = 'stats-event-dot--print-failed';
  } else if (e.type === 'upload_done') {
    dotCls = 'stats-event-dot--upload-done';
  } else if (e.type === 'upload_error') {
    dotCls = 'stats-event-dot--upload-error';
  }
  return `<div class="stats-event-item">
    <span class="stats-event-dot ${dotCls}"></span>
    <div class="stats-event-content">
      <span class="stats-event-label">${esc(e.label)}</span>
      <span class="stats-event-time">${esc(timeStr)}</span>
    </div>
  </div>`;
}

async function loadPrinterStatus() {
  const notice       = document.getElementById('printer-notice');
  const noticeText   = document.getElementById('printer-notice-text');
  const noticeAction = document.getElementById('printer-notice-action');
  const statsWrap    = document.getElementById('printer-stats-wrap');
  const badge        = document.getElementById('printer-state-badge');

  function setNotice(text, showAction = false) {
    noticeText.textContent = text;
    notice.classList.remove('hidden');
    noticeAction.classList.toggle('hidden', !showAction);
  }

  let data;
  try {
    data = await api('/api/printer/status');
  } catch (e) {
    if (lastPrinterData) {
      renderPrinterLive(lastPrinterData, true);
      setNotice('Lost connection to server — showing last known data');
    } else {
      badge.textContent  = 'OFFLINE';
      badge.className    = 'printer-badge printer-badge--unknown';
      statsWrap.classList.add('printer-offline');
      setNotice(`Could not reach server: ${e.message}`);
      hidePrinterControls();
    }
    return;
  }

  if (!data.configured) {
    badge.textContent = 'OFFLINE';
    badge.className   = 'printer-badge printer-badge--unknown';
    document.getElementById('printer-filename').textContent      = '';
    document.getElementById('printer-last-updated').textContent  = '';
    statsWrap.classList.add('printer-offline');
    setNotice('PrusaLink not configured — printer stats unavailable.', true);
    hidePrinterControls();
    return;
  }

  if (!data.reachable) {
    if (lastPrinterData) {
      renderPrinterLive(lastPrinterData, true);
      setNotice(`Printer unreachable — showing last known data${data.error ? ': ' + data.error : ''}`);
    } else {
      badge.textContent = 'OFFLINE';
      badge.className   = 'printer-badge printer-badge--unknown';
      statsWrap.classList.add('printer-offline');
      setNotice(data.error ? `Printer unreachable: ${data.error}` : 'Printer unreachable — check connection');
      hidePrinterControls();
    }
    return;
  }

  notice.classList.add('hidden');
  statsWrap.classList.remove('printer-offline');
  lastPrinterData = data;
  renderPrinterLive(data, false);
}

function renderPrinterLive(data, stale) {
  const p   = data.printer || {};
  const job = data.job;
  const state    = (p.state || 'UNKNOWN').toUpperCase();
  const isActive = state === 'PRINTING' || state === 'PAUSED';

  const badge = document.getElementById('printer-state-badge');
  badge.textContent = stale ? `${state} (stale)` : state;
  badge.className = 'printer-badge ' + printerStateBadgeClass(state);

  document.getElementById('printer-filename').textContent     = job?.display_name ?? '';
  document.getElementById('printer-last-updated').textContent = stale ? 'Last known data' : `Updated ${new Date().toLocaleTimeString()}`;

  const msgEl = document.getElementById('printer-message');
  const msgText = p.message || '';
  if (msgText) {
    document.getElementById('printer-message-text').textContent = msgText;
    msgEl.classList.remove('hidden');
    msgEl.style.color = (state === 'ERROR') ? 'var(--red)' : 'var(--yellow)';
    msgEl.style.background = (state === 'ERROR') ? 'rgba(248,81,73,.08)' : '';
    msgEl.style.borderBottomColor = (state === 'ERROR') ? 'rgba(248,81,73,.2)' : '';
  } else {
    msgEl.classList.add('hidden');
  }

  const fmt1 = v => (v != null ? `${v.toFixed(1)}°C` : '—');
  document.getElementById('ps-nozzle').textContent        = fmt1(p.temp_nozzle);
  document.getElementById('ps-nozzle-target').textContent = p.target_nozzle != null ? `/ ${p.target_nozzle.toFixed(0)}°C` : '';
  document.getElementById('ps-bed').textContent           = fmt1(p.temp_bed);
  document.getElementById('ps-bed-target').textContent    = p.target_bed != null ? `/ ${p.target_bed.toFixed(0)}°C` : '';
  document.getElementById('ps-z').textContent     = p.axis_z    != null ? `${p.axis_z.toFixed(2)} mm` : '—';
  document.getElementById('ps-speed').textContent = p.speed     != null ? `${p.speed}%`               : '—';
  document.getElementById('ps-flow').textContent  = p.flow      != null ? `${p.flow}%`                : '—';
  document.getElementById('ps-fan-hotend').textContent = p.fan_hotend != null ? `${p.fan_hotend} rpm` : '—';
  document.getElementById('ps-fan-print').textContent  = p.fan_print  != null ? `/ ${p.fan_print} rpm` : '';

  const jobPanel  = document.getElementById('printer-job-panel');
  const thumbEl   = document.getElementById('ps-thumbnail');
  if (isActive && job) {
    const pct = job.progress ?? 0;
    document.getElementById('printer-progress-bar').style.width = `${pct.toFixed(1)}%`;
    document.getElementById('ps-progress').textContent  = `${pct.toFixed(1)}%`;
    document.getElementById('ps-elapsed').textContent   = job.time_printing  != null ? fmtDuration(job.time_printing)  : '—';
    document.getElementById('ps-remaining').textContent = job.time_remaining != null ? `${fmtDuration(job.time_remaining)} remaining` : '—';
    document.getElementById('ps-job-name').textContent  = job.display_name ?? '';
    // Load thumbnail once per job; cache-bust by display_name so it refreshes on new jobs.
    const thumbKey = encodeURIComponent(job.display_name || 'active');
    if (thumbEl.dataset.job !== thumbKey) {
      thumbEl.dataset.job = thumbKey;
      thumbEl.classList.add('hidden');
      thumbEl.onerror = () => thumbEl.classList.add('hidden');
      thumbEl.onload  = () => thumbEl.classList.remove('hidden');
      thumbEl.src = `/api/printer/thumbnail?job=${thumbKey}`;
    }
    jobPanel.classList.remove('hidden');
  } else {
    document.getElementById('ps-job-name').textContent = '';
    thumbEl.src = '';
    thumbEl.dataset.job = '';
    thumbEl.classList.add('hidden');
    jobPanel.classList.add('hidden');
  }

  renderPrinterControls(state, stale);
}

function printerStateBadgeClass(state) {
  switch (state) {
    case 'PRINTING':  return 'printer-badge--printing';
    case 'PAUSED':    return 'printer-badge--paused';
    case 'IDLE':      return 'printer-badge--idle';
    case 'FINISHED':  return 'printer-badge--finished';
    case 'ERROR':
    case 'ATTENTION': return 'printer-badge--error';
    default:          return 'printer-badge--unknown';
  }
}

function fmtDuration(secs) {
  if (secs == null || secs < 0) return '—';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
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
    form.elements.orientation.value       = cam.orientation || 'landscape';
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
    orientation:       form.elements.orientation.value || 'landscape',
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
    f.elements.category_id.value         = d.category_id || '28';
    f.elements.keywords.value            = (d.keywords || []).join(', ');
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
  // Hide any previous step-2 so a stale code can't be accidentally re-submitted
  document.getElementById('yt-auth-step2').classList.add('hidden');
  document.getElementById('yt-redirect-paste').value = '';
  btn.disabled = true;
  try {
    const { auth_url } = await api('/api/youtube/auth/start', { method: 'POST' });
    document.getElementById('yt-auth-link').href = auth_url;
    document.getElementById('yt-auth-step2').classList.remove('hidden');
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
    console.error('YouTube auth complete failed:', e.message);
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
        category_id:         form.elements.category_id.value,
        keywords:            form.elements.keywords.value.split(',').map(s => s.trim()).filter(Boolean),
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

  try {
    const [recs, uploads, authStatus] = await Promise.all([
      api('/api/recordings'),
      api('/api/uploads/statuses').catch(() => ({})),
      api('/api/youtube/auth/status').catch(() => ({ authorized: false })),
    ]);

    const key = JSON.stringify({ recs, uploads, authorized: authStatus.authorized });
    if (key === lastRecordingsKey) return;
    lastRecordingsKey = key;

    if (recs.length === 0) {
      list.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    list.innerHTML = recs.map(r => buildRecordingRow(r, uploads[r.name], authStatus.authorized)).join('');
  } catch (e) {
    list.innerHTML = `<p style="color:var(--red);padding:8px">Error: ${esc(e.message)}</p>`;
  }
}

function buildRecordingRow(r, upload, ytAuthorized) {
  const title    = r.display_name ? esc(r.display_name) : esc(r.name);
  const subtitle = r.display_name ? `<div class="rec-filename">${esc(r.name)}</div>` : '';

  if (r.live) {
    return `
      <div class="rec-item rec-item--live">
        <div class="rec-live-dot"><span class="rec-dot"></span></div>
        <div class="rec-info">
          <div class="rec-name">${title}</div>
          ${subtitle}
          <div class="rec-meta">Recording in progress&hellip;</div>
        </div>
        <div class="rec-actions">
          <button class="btn btn-danger btn-sm" data-action="stop-rec" data-cam="${esc(r.camera_name)}">&#9632; Stop</button>
        </div>
      </div>`;
  }

  if (r.deleted) {
    const ytLink = upload?.status === 'done' && upload.url
      ? `<a href="${esc(upload.url)}" target="_blank" class="btn btn-ghost btn-sm yt-done-btn">&#9654; YouTube</a>`
      : '';
    const meta = r.size ? `${fmtBytes(r.size)} &middot; ` : '';
    return `
      <div class="rec-item rec-item--deleted">
        <div class="rec-info">
          <div class="rec-name">${title}</div>
          ${subtitle}
          <div class="rec-meta">${meta}File deleted</div>
        </div>
        <div class="rec-actions">${ytLink}</div>
      </div>`;
  }

  let ytBtn = '';
  if (upload) {
    if (upload.status === 'pending' || upload.status === 'uploading') {
      const pct = upload.status === 'uploading' ? ` ${upload.pct}%` : '';
      ytBtn = `<span class="badge badge-uploading">Uploading${pct}</span>`;
    } else if (upload.status === 'done' && upload.url) {
      ytBtn = `<a href="${esc(upload.url)}" target="_blank" class="btn btn-ghost btn-sm yt-done-btn">&#9654; YouTube</a>`;
    } else if (upload.status === 'error') {
      ytBtn = `<button class="btn btn-ghost btn-sm" data-action="upload-rec" data-file="${esc(r.name)}" title="${esc(upload.error || '')}">Retry</button>`;
    }
  } else if (ytAuthorized) {
    ytBtn = `<button class="btn btn-ghost btn-sm" data-action="upload-rec" data-file="${esc(r.name)}">&#8593; YouTube</button>`;
  }

  return `
    <div class="rec-item">
      <div class="rec-info">
        <div class="rec-name">${title}</div>
        ${subtitle}
        <div class="rec-meta">${fmtBytes(r.size)} &middot; ${fmtDate(r.mtime)}</div>
      </div>
      <div class="rec-actions">
        ${ytBtn}
        <button class="btn btn-ghost btn-sm btn-danger" data-action="delete-rec" data-file="${esc(r.name)}">Delete</button>
      </div>
    </div>`;
}

function startRecordingsRefresh() {
  stopRecordingsRefresh();
  recordingsRefreshTimer = setInterval(loadRecordings, 4_000);
}

function stopRecordingsRefresh() {
  if (recordingsRefreshTimer !== null) { clearInterval(recordingsRefreshTimer); recordingsRefreshTimer = null; }
  lastRecordingsKey = null;
}

function startStatsRefresh() {
  stopStatsRefresh();
  statsRefreshTimer = setInterval(loadStats, 30_000);
}

function stopStatsRefresh() {
  if (statsRefreshTimer !== null) { clearInterval(statsRefreshTimer); statsRefreshTimer = null; }
}

async function startManualRecording(cameraName) {
  try {
    await api(`/api/recording-status/start/${encodeURIComponent(cameraName)}`, { method: 'POST' });
    toast(`Recording started for ${cameraName}`, 'success');
    refreshRecordingStatus();
  } catch (e) {
    toast(`Start failed: ${e.message}`, 'error');
  }
}

async function stopLiveRecording(cameraName) {
  try {
    await api(`/api/recording-status/stop/${encodeURIComponent(cameraName)}`, { method: 'POST' });
    toast('Recording stopped', 'success');
    loadRecordings();
  } catch (e) {
    toast(`Stop failed: ${e.message}`, 'error');
  }
}

async function uploadRecording(filename) {
  try {
    await api(`/api/recordings/${encodeURIComponent(filename)}/upload`, { method: 'POST' });
    toast('Upload started', 'success');
    loadRecordings();
  } catch (e) {
    toast(`Upload failed: ${e.message}`, 'error');
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

// ── Printer controls ──────────────────────────────────────────────────────────

function hidePrinterControls() {
  printerConfirmPending = null;
  const el = document.getElementById('printer-controls');
  if (el) el.classList.add('hidden');
}

function renderPrinterControls(state, stale) {
  const el = document.getElementById('printer-controls');
  if (!el) return;

  if (stale) { hidePrinterControls(); return; }

  const isPrinting = state === 'PRINTING';
  const isPaused   = state === 'PAUSED';
  const isActive   = isPrinting || isPaused;

  if (printerConfirmPending) {
    el.classList.remove('hidden');
    el.innerHTML = `
      <span class="printer-confirm-label">${esc(printerConfirmPending.label)}</span>
      <button class="btn btn-danger btn-sm" id="printer-confirm-yes">Confirm</button>
      <button class="btn btn-ghost btn-sm" id="printer-confirm-cancel">Cancel</button>
    `;
    document.getElementById('printer-confirm-yes').onclick = () => {
      const fn = printerConfirmPending.fn;
      printerConfirmPending = null;
      fn();
    };
    document.getElementById('printer-confirm-cancel').onclick = () => {
      printerConfirmPending = null;
      renderPrinterControls(state, stale);
    };
    return;
  }

  el.classList.remove('hidden');
  el.innerHTML = `
    ${isPrinting ? '<button class="btn btn-ghost btn-sm" id="pc-pause">&#9646;&#9646; Pause</button>' : ''}
    ${isPaused   ? '<button class="btn btn-ghost btn-sm" id="pc-resume">&#9654; Resume</button>' : ''}
    ${isActive   ? '<button class="btn btn-ghost btn-sm btn-danger" id="pc-stop">&#9632; Stop</button>' : ''}
    <button class="btn btn-ghost btn-sm" id="pc-upload">&#8593; Upload file</button>
    <button class="btn btn-ghost btn-sm" id="pc-browse">&#128193; Files</button>
  `;

  if (isPrinting) document.getElementById('pc-pause').onclick  = () => startPrinterConfirm('Pause the print?', printerPause, state, stale);
  if (isPaused)   document.getElementById('pc-resume').onclick = () => startPrinterConfirm('Resume the print?', printerResume, state, stale);
  if (isActive)   document.getElementById('pc-stop').onclick   = () => startPrinterConfirm('Stop the print? This cannot be undone.', printerStop, state, stale);
  document.getElementById('pc-upload').onclick = openUploadModal;
  document.getElementById('pc-browse').onclick = togglePrinterFiles;
}

function startPrinterConfirm(label, fn, state, stale) {
  printerConfirmPending = { label, fn };
  renderPrinterControls(state, stale);
}

async function printerPause() {
  try {
    await api('/api/printer/control/pause', { method: 'POST' });
    toast('Print paused', 'success');
  } catch (e) { toast(`Pause failed: ${e.message}`, 'error'); }
  loadPrinterStatus();
}

async function printerResume() {
  try {
    await api('/api/printer/control/resume', { method: 'POST' });
    toast('Print resumed', 'success');
  } catch (e) { toast(`Resume failed: ${e.message}`, 'error'); }
  loadPrinterStatus();
}

async function printerStop() {
  try {
    await api('/api/printer/control/stop', { method: 'POST' });
    toast('Print stopped', 'success');
  } catch (e) { toast(`Stop failed: ${e.message}`, 'error'); }
  loadPrinterStatus();
}

// ── Printer file browser ──────────────────────────────────────────────────────

function togglePrinterFiles() {
  printerFilesOpen = !printerFilesOpen;
  document.getElementById('printer-files-panel').classList.toggle('hidden', !printerFilesOpen);
  if (printerFilesOpen) loadPrinterFiles('usb');
}

function closePrinterFiles() {
  printerFilesOpen = false;
  document.getElementById('printer-files-panel').classList.add('hidden');
}

async function loadPrinterFiles(storage, force = false) {
  const list = document.getElementById('printer-files-list');
  if (force) list.innerHTML = '<div class="printer-files-msg">Refreshing from printer…</div>';

  let files;
  try {
    const url = `/api/printer/files/${encodeURIComponent(storage)}${force ? '?refresh=true' : ''}`;
    files = await api(url);
  } catch (e) {
    list.innerHTML = `<div class="printer-files-msg printer-files-err">Error: ${esc(e.message)}</div>`;
    return;
  }

  if (!files.length) {
    list.innerHTML = `<div class="printer-files-msg">No print files found on ${esc(storage)}.</div>`;
    return;
  }

  list.innerHTML = files.map(f => {
    const iconUrl = `/api/printer/file-icon/${f.storage}/${f.path.split('/').map(encodeURIComponent).join('/')}`;
    const iconHtml = `<img class="printer-file-icon" src="${iconUrl}" alt="" loading="lazy"
      onerror="this.outerHTML='<div class=\\'printer-file-icon printer-file-icon--placeholder\\'>◻</div>'">`;

    const sizeTxt = f.size ? fmtBytes(f.size) : '';
    const dateTxt = f.timestamp ? fmtDate(f.timestamp) : '';
    const meta    = [sizeTxt, dateTxt].filter(Boolean).join(' · ');

    let printMeta = '';
    if (f.print_count > 0) {
      const lastDate = f.last_print_ts
        ? new Date(f.last_print_ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
        : '';
      const stateStr = f.last_print_state ? ` · ${f.last_print_state}` : '';
      printMeta = `<div class="printer-file-prints">Printed ${f.print_count}×${lastDate ? ' · Last: ' + lastDate : ''}${stateStr}</div>`;
    }

    return `
    <div class="printer-file-item">
      ${iconHtml}
      <div class="printer-file-info">
        <div class="printer-file-name printer-file-name--link"
             data-action="preview-file"
             data-storage="${esc(f.storage)}"
             data-path="${esc(f.path)}"
             data-display-name="${esc(f.display_name)}"
             data-timestamp="${f.timestamp || ''}"
             title="${esc(f.path)}">${esc(f.display_name)}</div>
        <div class="printer-file-meta">${meta}</div>
        ${printMeta}
      </div>
      <button class="btn btn-primary btn-sm"
              data-action="print-file"
              data-storage="${esc(f.storage)}"
              data-path="${esc(f.path)}"
              data-name="${esc(f.display_name)}">&#9654; Print</button>
    </div>`;
  }).join('');
}

function openFilePreview(dataset) {
  const { storage, path, displayName, timestamp } = dataset;
  const iconUrl = `/api/printer/file-icon/${storage}/${path.split('/').map(encodeURIComponent).join('/')}`;

  const img  = document.getElementById('file-preview-img');
  const name = document.getElementById('file-preview-name');
  const meta = document.getElementById('file-preview-meta');

  img.src = '';
  img.classList.add('hidden');
  name.textContent = displayName;
  meta.textContent = timestamp ? fmtDate(Number(timestamp)) : '';

  img.onload  = () => img.classList.remove('hidden');
  img.onerror = () => img.classList.add('hidden');
  img.src = iconUrl;

  document.getElementById('file-preview-overlay').classList.remove('hidden');
}

function closeFilePreview() {
  document.getElementById('file-preview-overlay').classList.add('hidden');
  document.getElementById('file-preview-img').src = '';
}

async function printFile(storage, path, name) {
  if (!confirm(`Start printing "${name}"?`)) return;
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  try {
    await api(`/api/printer/files/${encodeURIComponent(storage)}/${encodedPath}/print`, { method: 'POST' });
    toast(`Print started: ${name}`, 'success');
    closePrinterFiles();
    loadPrinterStatus();
  } catch (e) {
    toast(`Failed to start print: ${e.message}`, 'error');
  }
}

// ── File upload modal ─────────────────────────────────────────────────────────

function openUploadModal() {
  document.getElementById('upload-file-input').value = '';
  document.getElementById('upload-print-after').checked = false;
  document.getElementById('upload-status').classList.add('hidden');
  document.getElementById('upload-progress-wrap').classList.add('hidden');
  document.getElementById('upload-progress').value = 0;
  const btn = document.getElementById('upload-confirm-btn');
  btn.disabled = false;
  btn.textContent = 'Upload';
  document.getElementById('upload-overlay').classList.remove('hidden');
  document.getElementById('upload-file-input').focus();
}

function closeUploadModal() {
  document.getElementById('upload-overlay').classList.add('hidden');
}

function doUpload() {
  const fileInput = document.getElementById('upload-file-input');
  if (!fileInput.files.length) { toast('Select a file first', 'error'); return; }

  const file        = fileInput.files[0];
  const printAfter  = document.getElementById('upload-print-after').checked;
  const storage     = 'usb';
  const btn         = document.getElementById('upload-confirm-btn');
  const statusEl    = document.getElementById('upload-status');
  const progressWrap = document.getElementById('upload-progress-wrap');
  const progressBar  = document.getElementById('upload-progress');
  const progressLbl  = document.getElementById('upload-progress-label');

  btn.disabled = true;
  btn.textContent = 'Uploading…';
  statusEl.textContent = `Uploading ${file.name}…`;
  statusEl.classList.remove('hidden');
  progressBar.value = 0;
  progressLbl.textContent = '0%';
  progressWrap.classList.remove('hidden');

  const form = new FormData();
  form.append('file', file);
  form.append('storage', storage);
  form.append('print_after_upload', String(printAfter));

  const xhr = new XMLHttpRequest();

  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    progressBar.value = pct;
    progressLbl.textContent = `${pct}%`;
    if (pct === 100) {
      statusEl.textContent = `Processing ${file.name} on printer…`;
    }
  };

  xhr.onload = () => {
    progressWrap.classList.add('hidden');
    if (xhr.status >= 200 && xhr.status < 300) {
      toast(printAfter ? `${file.name} uploaded — print starting` : `${file.name} uploaded`, 'success');
      closeUploadModal();
    } else {
      let msg = `HTTP ${xhr.status}`;
      try { const err = JSON.parse(xhr.responseText); msg = err.detail || msg; } catch {}
      toast(`Upload failed: ${msg}`, 'error');
      btn.disabled = false;
      btn.textContent = 'Upload';
      statusEl.classList.add('hidden');
    }
  };

  xhr.onerror = () => {
    progressWrap.classList.add('hidden');
    toast('Upload failed: network error', 'error');
    btn.disabled = false;
    btn.textContent = 'Upload';
    statusEl.classList.add('hidden');
  };

  xhr.open('POST', '/api/printer/upload');
  xhr.send(form);
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
