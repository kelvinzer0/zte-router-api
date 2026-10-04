'use strict';

/**
 * ZTE Router Console - Clean, Non-Modal Web UI Controller
 */

const state = {
  routers: [],
  selectedId: null,
  currentOverview: null,
  timer: null,
  isTesting: false,
};

// Utilities
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(val) {
  if (val === null || val === undefined || val === '') return '-';
  return String(val)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function showAlert(message, isError = false) {
  const banner = $('#alert-banner');
  const msgEl = $('#alert-message');
  banner.className = `alert ${isError ? 'alert-danger' : 'alert-success'}`;
  msgEl.textContent = message;
  banner.style.display = 'flex';
}

function hideAlert() {
  $('#alert-banner').style.display = 'none';
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return '-';
  const b = Number(bytes);
  if (b === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let val = b;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  let body = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { detail: text };
    }
  }

  if (!res.ok) {
    const errorMsg = (body && (body.detail || body.message)) || `HTTP ${res.status}`;
    throw new Error(typeof errorMsg === 'string' ? errorMsg : JSON.stringify(errorMsg));
  }
  return body;
}

// Router Management
async function loadRouters() {
  try {
    const data = await api('/routers');
    state.routers = data.routers || [];

    const select = $('#router-select');
    select.innerHTML = '';

    if (state.routers.length === 0) {
      select.innerHTML = '<option value="" disabled selected>Belum ada router terdaftar</option>';
      state.selectedId = null;
      updateStatusBadge('offline', 'Belum ada router');
      return;
    }

    state.routers.forEach((r) => {
      const opt = document.createElement('option');
      opt.value = r.id;
      opt.textContent = `${r.name} (${r.address})${r.location ? ' - ' + r.location : ''}`;
      select.appendChild(opt);
    });

    const remembered = localStorage.getItem('zte_selected_router');
    if (remembered && state.routers.some((r) => r.id === remembered)) {
      state.selectedId = remembered;
    } else {
      state.selectedId = state.routers[0].id;
    }
    select.value = state.selectedId;

    await refreshRouterData();
    testRouterConnection(state.selectedId);
  } catch (err) {
    showAlert('Gagal memuat daftar router: ' + err.message, true);
  }
}

async function refreshRouterData() {
  if (!state.selectedId) return;

  const loading = $('#loading-state');
  loading.style.display = 'block';

  try {
    const overview = await api(`/routers/${state.selectedId}/overview`);
    state.currentOverview = overview;
    renderOverview(overview);
    loading.style.display = 'none';
  } catch (err) {
    loading.style.display = 'none';
    showAlert(`Gagal mengambil data router: ${err.message}`, true);
    updateStatusBadge('offline', 'Gagal memuat data');
  }
}

function updateStatusBadge(type, label) {
  const badge = $('#router-status-badge');
  badge.className = 'badge';
  let dotClass = 'dot-gray';

  if (type === 'online') {
    badge.classList.add('badge-online');
    dotClass = 'dot-green';
  } else if (type === 'warn') {
    badge.classList.add('badge-warn');
    dotClass = 'dot-yellow';
  } else if (type === 'offline') {
    badge.classList.add('badge-offline');
    dotClass = 'dot-red';
  } else {
    badge.classList.add('badge-neutral');
  }

  badge.innerHTML = `<span class="dot ${dotClass}"></span> ${esc(label)}`;
}

async function testRouterConnection(routerId) {
  if (!routerId || state.isTesting) return;
  state.isTesting = true;
  updateStatusBadge('neutral', 'Menguji koneksi...');

  try {
    const res = await api(`/routers/${routerId}/test`, { method: 'POST' });
    if (res.reachable) {
      updateStatusBadge('online', `Online (${res.latency_ms}ms)${res.model ? ' - ' + res.model : ''}`);
    } else {
      updateStatusBadge('offline', `Gagal: ${res.message || 'Offline'}`);
    }
  } catch (err) {
    updateStatusBadge('offline', 'Tidak dapat dihubungi');
  } finally {
    state.isTesting = false;
  }
}

async function reauthRouter(routerId) {
  if (!routerId) return;
  updateStatusBadge('neutral', 'Otentikasi ulang...');

  try {
    const res = await api(`/routers/${routerId}/reauth`, { method: 'POST' });
    if (res.success) {
      updateStatusBadge('online', `Re-auth OK (${res.latency_ms}ms)${res.model ? ' - ' + res.model : ''}`);
      showAlert(`Otentikasi ulang ke ${res.model || 'router'} berhasil! Memperbarui data...`);
      await refreshRouterData();
    } else {
      updateStatusBadge('offline', `Re-auth gagal: ${res.message}`);
      showAlert(res.message, true);
    }
  } catch (err) {
    updateStatusBadge('offline', 'Re-auth gagal');
    showAlert('Otentikasi ulang gagal: ' + err.message, true);
  }
}

// Render Overview & Metrics
function renderOverview(data) {
  // 1. WiFi Networks
  renderWiFi(data.wifi || []);

  // 2. Optical PON
  renderPON(data.pon);

  // 3. Device Info
  renderDeviceInfo(data.device, data.router);

  // 4. WAN Interfaces
  renderWAN(data.wan || []);

  // 5. LAN Ports
  renderLAN(data.lan || []);

  // 6. Raw JSON
  $('#json-raw-content').textContent = JSON.stringify(data, null, 2);
}

function renderWiFi(wifiList) {
  const container = $('#wifi-grid');
  const countBadge = $('#wifi-count-badge');
  countBadge.textContent = `${wifiList.length} SSID`;

  if (!wifiList || wifiList.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted); padding:1rem;">Tidak ada jaringan WiFi terdeteksi.</div>';
    return;
  }

  container.innerHTML = wifiList
    .map((w, idx) => {
      const activeClass = w.enabled ? 'active-net' : '';
      const statusBadge = w.enabled
        ? '<span class="badge badge-online">Aktif</span>'
        : '<span class="badge badge-neutral">Nonaktif</span>';
      const pass = w.password || '';

      return `
        <div class="wifi-item ${activeClass}">
          <div class="wifi-item-header">
            <div>
              <div class="wifi-ssid">${esc(w.ssid)}</div>
              <div class="wifi-meta">
                <span>Band: ${esc(w.band || '2.4G')}</span> &bull;
                <span>Keamanan: ${esc(w.security || 'WPA/WPA2-PSK')}</span>
              </div>
            </div>
            <div>${statusBadge}</div>
          </div>

          <div>
            <label style="font-size:0.75rem; color:var(--text-muted);">Kata Sandi / Password:</label>
            <div class="wifi-pass-row">
              <span class="wifi-pass-text" id="wifi-pass-${idx}">${pass ? esc(pass) : '<em>(Tidak ada sandi / Terbuka)</em>'}</span>
              ${
                pass
                  ? `<button class="btn btn-sm" onclick="copyPassword('${esc(pass).replace(/'/g, "\\'")}', this)" title="Salin password ke clipboard">
                      📋 Salin
                    </button>`
                  : ''
              }
            </div>
          </div>
        </div>
      `;
    })
    .join('');
}

window.copyPassword = async function (password, btn) {
  try {
    await navigator.clipboard.writeText(password);
    const originalText = btn.innerHTML;
    btn.innerHTML = '✅ Tersalin!';
    btn.classList.add('btn-primary');
    setTimeout(() => {
      btn.innerHTML = originalText;
      btn.classList.remove('btn-primary');
    }, 2000);
  } catch (err) {
    showAlert('Gagal menyalin: ' + err.message, true);
  }
};

function renderPON(pon) {
  const rxValEl = $('#pon-rx-val');
  const txValEl = $('#pon-tx-val');
  const tempValEl = $('#pon-temp-val');
  const voltValEl = $('#pon-volt-val');
  const currValEl = $('#pon-curr-val');
  const onuBadge = $('#pon-onu-badge');

  if (!pon) {
    rxValEl.innerHTML = `-<span class="metric-unit">dBm</span>`;
    txValEl.innerHTML = `-<span class="metric-unit">dBm</span>`;
    tempValEl.innerHTML = `-<span class="metric-unit">°C</span>`;
    voltValEl.innerHTML = `-<span class="metric-unit">mV</span>`;
    currValEl.innerHTML = `-<span class="metric-unit">mA</span>`;
    onuBadge.textContent = 'Status: N/A';
    onuBadge.className = 'badge badge-neutral';
    return;
  }

  // ONU State
  const onuState = pon.onu_state || 'Unknown';
  onuBadge.textContent = onuState;
  if (onuState.includes('O5') || onuState.includes('Operation')) {
    onuBadge.className = 'badge badge-online';
  } else {
    onuBadge.className = 'badge badge-warn';
  }

  // Rx Power with threshold color
  const rx = pon.rx_power_dbm;
  let rxClass = '';
  if (rx !== null && rx !== undefined) {
    if (rx > -25) rxClass = 'val-good';
    else if (rx >= -27) rxClass = 'val-warn';
    else rxClass = 'val-bad';
    rxValEl.className = `metric-value ${rxClass}`;
    rxValEl.innerHTML = `${rx.toFixed(2)}<span class="metric-unit">dBm</span>`;
  } else {
    rxValEl.className = 'metric-value';
    rxValEl.innerHTML = `-<span class="metric-unit">dBm</span>`;
  }

  // Tx Power
  const tx = pon.tx_power_dbm;
  txValEl.innerHTML = tx !== null && tx !== undefined ? `${tx.toFixed(2)}<span class="metric-unit">dBm</span>` : `-<span class="metric-unit">dBm</span>`;

  // Temp
  const temp = pon.temperature_c;
  tempValEl.innerHTML = temp !== null && temp !== undefined ? `${temp}<span class="metric-unit">°C</span>` : `-<span class="metric-unit">°C</span>`;

  // Volt
  const volt = pon.voltage_mv;
  voltValEl.innerHTML = volt !== null && volt !== undefined ? `${volt}<span class="metric-unit">mV</span>` : `-<span class="metric-unit">mV</span>`;

  // Curr
  const curr = pon.current_ma;
  currValEl.innerHTML = curr !== null && curr !== undefined ? `${curr.toFixed(2)}<span class="metric-unit">mA</span>` : `-<span class="metric-unit">mA</span>`;
}

function renderDeviceInfo(dev, router) {
  $('#dev-model').textContent = (dev && dev.model) || (router && router.name) || '-';
  $('#dev-sn').textContent = (dev && dev.serial_number) || '-';
  $('#dev-hw').textContent = (dev && dev.hardware_version) || '-';
  $('#dev-sw').textContent = (dev && dev.software_version) || '-';
  $('#dev-boot').textContent = (dev && dev.boot_version) || '-';
  $('#dev-url').textContent = (router && router.url) || '-';
}

function renderWAN(wanList) {
  const tbody = $('#wan-tbody');
  if (!wanList || wanList.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; color:var(--text-muted);">Tidak ada koneksi WAN terdaftar</td></tr>';
    return;
  }

  tbody.innerHTML = wanList
    .map((w) => {
      const isConnected = (w.status || '').toLowerCase() === 'connected';
      const statusPill = isConnected
        ? '<span class="badge badge-online">Connected</span>'
        : `<span class="badge badge-neutral">${esc(w.status || 'Disconnected')}</span>`;

      const userHtml = w.username
        ? `<div style="display:inline-flex; align-items:center; gap:6px;">
             <code style="color:var(--text-main); font-weight:600;">${esc(w.username)}</code>
             <button class="btn btn-sm btn-outline" style="padding:1px 6px; font-size:11px;" title="Salin Username" onclick="navigator.clipboard.writeText('${esc(w.username)}'); this.textContent='✓'; setTimeout(()=>this.textContent='📋', 1500);">📋</button>
           </div>`
        : '<span style="color:var(--text-muted);">-</span>';

      const passHtml = w.password
        ? `<div style="display:inline-flex; align-items:center; gap:6px;">
             <code style="color:var(--accent); font-weight:600;">${esc(w.password)}</code>
             <button class="btn btn-sm btn-outline" style="padding:1px 6px; font-size:11px;" title="Salin Password" onclick="navigator.clipboard.writeText('${esc(w.password)}'); this.textContent='✓'; setTimeout(()=>this.textContent='📋', 1500);">📋</button>
           </div>`
        : '<span style="color:var(--text-muted);">-</span>';

      return `
        <tr>
          <td><strong>${esc(w.name)}</strong></td>
          <td>${esc(w.connection_type || 'IP')}</td>
          <td>${statusPill}</td>
          <td><code>${esc(w.ip_address)}</code></td>
          <td>${userHtml}</td>
          <td>${passHtml}</td>
          <td><code>${esc(w.gateway)}</code></td>
          <td>${esc(w.dns_servers)}</td>
          <td><small>${esc(w.mac_address)}</small></td>
        </tr>
      `;
    })
    .join('');
}

function renderLAN(lanList) {
  const tbody = $('#lan-tbody');
  if (!lanList || lanList.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted);">Tidak ada port LAN terdeteksi</td></tr>';
    return;
  }

  tbody.innerHTML = lanList
    .map((p) => {
      const isUp = (p.link || '').toLowerCase() === 'up';
      const statusPill = isUp
        ? '<span class="badge badge-online">Up</span>'
        : '<span class="badge badge-neutral">Down</span>';

      return `
        <tr>
          <td><strong>${esc(p.name)}</strong></td>
          <td>${statusPill}</td>
          <td>${p.speed ? esc(p.speed) + ' Mbps' : '-'}</td>
          <td>${esc(p.duplex)}</td>
          <td>${p.bytes_rx !== null && p.bytes_rx !== undefined ? formatBytes(p.bytes_rx) : '-'}</td>
          <td>${p.bytes_tx !== null && p.bytes_tx !== undefined ? formatBytes(p.bytes_tx) : '-'}</td>
        </tr>
      `;
    })
    .join('');
}

// Inline Form Handlers (NON-MODAL)
function openAddForm() {
  $('#form-title').textContent = 'Tambah Router Baru';
  $('#form-id').value = '';
  $('#form-name').value = '';
  $('#form-host').value = '';
  $('#form-port').value = '80';
  $('#form-username').value = 'admin';
  $('#form-password').value = '';
  $('#form-timeout').value = '10';
  $('#form-location').value = '';
  $('#form-notes').value = '';
  $('#router-form-card').style.display = 'block';
  $('#router-form-card').scrollIntoView({ behavior: 'smooth' });
}

function openEditForm() {
  const current = state.routers.find((r) => r.id === state.selectedId);
  if (!current) {
    showAlert('Pilih router terlebih dahulu untuk diedit', true);
    return;
  }

  $('#form-title').textContent = `Edit Router: ${current.name}`;
  $('#form-id').value = current.id;
  $('#form-name').value = current.name || '';
  $('#form-host').value = current.host || '';
  $('#form-port').value = current.port || 80;
  $('#form-username').value = current.username || 'admin';
  $('#form-password').value = '';
  $('#form-timeout').value = current.timeout || 10;
  $('#form-location').value = current.location || '';
  $('#form-notes').value = current.notes || '';
  $('#router-form-card').style.display = 'block';
  $('#router-form-card').scrollIntoView({ behavior: 'smooth' });
}

function closeForm() {
  $('#router-form-card').style.display = 'none';
}

async function handleFormSubmit(e) {
  e.preventDefault();
  const id = $('#form-id').value.trim();
  const payload = {
    name: $('#form-name').value.trim(),
    host: $('#form-host').value.trim(),
    port: parseInt($('#form-port').value, 10) || 80,
    username: $('#form-username').value.trim() || 'admin',
    password: $('#form-password').value,
    timeout: parseInt($('#form-timeout').value, 10) || 10,
    location: $('#form-location').value.trim(),
    notes: $('#form-notes').value.trim(),
  };

  if (!payload.name || !payload.host) {
    showAlert('Nama router dan host/IP wajib diisi', true);
    return;
  }

  try {
    if (id) {
      await api(`/routers/${id}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      showAlert(`Router '${payload.name}' berhasil diperbarui`);
    } else {
      const res = await api('/routers', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      state.selectedId = res.id;
      localStorage.setItem('zte_selected_router', res.id);
      showAlert(`Router '${payload.name}' berhasil ditambahkan`);
    }
    closeForm();
    await loadRouters();
  } catch (err) {
    showAlert('Gagal menyimpan router: ' + err.message, true);
  }
}

// Inline Delete Confirmation
function promptDeleteRouter() {
  const current = state.routers.find((r) => r.id === state.selectedId);
  if (!current) return;

  $('#delete-confirm-text').innerHTML = `Yakin ingin menghapus router <strong>${esc(current.name)}</strong> (${esc(current.address)})?`;
  $('#delete-confirm-box').style.display = 'flex';
}

async function confirmDeleteRouter() {
  if (!state.selectedId) return;
  try {
    await api(`/routers/${state.selectedId}`, { method: 'DELETE' });
    showAlert('Router berhasil dihapus');
    $('#delete-confirm-box').style.display = 'none';
    state.selectedId = null;
    localStorage.removeItem('zte_selected_router');
    await loadRouters();
  } catch (err) {
    showAlert('Gagal menghapus router: ' + err.message, true);
  }
}

// Auto-refresh Timer
function setRefreshInterval() {
  if (state.timer) clearInterval(state.timer);
  const seconds = parseInt($('#refresh-interval').value, 10);
  if (seconds > 0) {
    state.timer = setInterval(() => {
      refreshRouterData();
    }, seconds * 1000);
  }
}

// Event Bindings
function initEvents() {
  $('#router-select').addEventListener('change', (e) => {
    state.selectedId = e.target.value;
    localStorage.setItem('zte_selected_router', state.selectedId);
    $('#delete-confirm-box').style.display = 'none';
    closeForm();
    refreshRouterData();
    testRouterConnection(state.selectedId);
  });

  $('#btn-test-conn').addEventListener('click', () => {
    testRouterConnection(state.selectedId);
  });

  $('#btn-reauth').addEventListener('click', () => {
    reauthRouter(state.selectedId);
  });

  $('#btn-refresh').addEventListener('click', () => {
    refreshRouterData();
    testRouterConnection(state.selectedId);
  });

  $('#refresh-interval').addEventListener('change', setRefreshInterval);

  $('#btn-toggle-add').addEventListener('click', openAddForm);
  $('#btn-edit-router').addEventListener('click', openEditForm);
  $('#btn-cancel-form').addEventListener('click', closeForm);
  $('#router-form').addEventListener('submit', handleFormSubmit);

  $('#btn-delete-router').addEventListener('click', promptDeleteRouter);
  $('#btn-confirm-delete').addEventListener('click', confirmDeleteRouter);
  $('#btn-cancel-delete').addEventListener('click', () => {
    $('#delete-confirm-box').style.display = 'none';
  });

  // Tab switching
  $('#section-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;
    const targetId = btn.dataset.tab;

    $$('.tab-btn').forEach((b) => b.classList.toggle('active', b === btn));
    $$('.tab-content').forEach((c) => c.classList.toggle('active', c.id === targetId));
  });
}

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  loadRouters();
  setRefreshInterval();
});