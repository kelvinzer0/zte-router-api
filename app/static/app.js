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

// Router Management & Dropdown Filter
function populateRouterSelect(filterText = '') {
  const select = $('#router-select');
  const clearBtn = $('#btn-clear-router-filter');
  const countBadge = $('#router-filter-count');
  const query = (filterText || '').trim().toLowerCase();

  if (clearBtn) {
    clearBtn.style.display = query ? 'block' : 'none';
  }

  select.innerHTML = '';

  if (!state.routers || state.routers.length === 0) {
    select.innerHTML = '<option value="" disabled selected>Belum ada router terdaftar</option>';
    if (countBadge) countBadge.style.display = 'none';
    return [];
  }

  const filtered = query
    ? state.routers.filter((r) => {
        const text = `${r.name || ''} ${r.address || ''} ${r.host || ''} ${r.ssid || ''} ${r.location || ''} ${r.model || ''}`.toLowerCase();
        return text.includes(query);
      })
    : state.routers;

  if (countBadge) {
    if (query) {
      countBadge.style.display = 'inline-block';
      countBadge.textContent = `${filtered.length} / ${state.routers.length}`;
      countBadge.className = filtered.length > 0 ? 'badge badge-online' : 'badge badge-offline';
    } else {
      countBadge.style.display = 'none';
    }
  }

  if (filtered.length === 0) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.disabled = true;
    opt.selected = true;
    opt.textContent = `❌ Tidak ada cocok: "${filterText}"`;
    select.appendChild(opt);
    return [];
  }

  filtered.forEach((r) => {
    const opt = document.createElement('option');
    opt.value = r.id;
    let label = r.name || r.address;
    if (!label.includes(r.address)) {
      label += ` (${r.address})`;
    }
    if (r.ssid) {
      label += ` [SSID: ${r.ssid}]`;
    }
    if (r.location) {
      label += ` - ${r.location}`;
    }
    opt.textContent = label;
    select.appendChild(opt);
  });

  return filtered;
}

async function loadRouters() {
  try {
    const data = await api('/routers');
    state.routers = data.routers || [];

    const filterVal = $('#router-filter-input')?.value || '';
    const filtered = populateRouterSelect(filterVal);

    if (state.routers.length === 0) {
      state.selectedId = null;
      updateStatusBadge('offline', 'Belum ada router');
      return;
    }

    const select = $('#router-select');
    const remembered = localStorage.getItem('zte_selected_router');

    let targetId = null;
    if (remembered && filtered.some((r) => r.id === remembered)) {
      targetId = remembered;
    } else if (state.selectedId && filtered.some((r) => r.id === state.selectedId)) {
      targetId = state.selectedId;
    } else if (filtered.length > 0) {
      targetId = filtered[0].id;
    }

    if (targetId) {
      state.selectedId = targetId;
      select.value = targetId;
      await refreshRouterData();
      testRouterConnection(state.selectedId);
    }
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

// Bulk Re-Authentication for All Routers
let reauthAllAbortController = null;

function closeReauthAll() {
  if (reauthAllAbortController) {
    reauthAllAbortController.abort();
    reauthAllAbortController = null;
  }
  const box = $('#reauth-all-progress-box');
  if (box) box.style.display = 'none';
}

function stopReauthAll() {
  if (reauthAllAbortController) {
    reauthAllAbortController.abort();
    reauthAllAbortController = null;
    $('#reauth-all-status-text').textContent = '⏹️ Re-Auth Massal dihentikan oleh pengguna.';
    const stopBtn = $('#btn-stop-reauth-all');
    if (stopBtn) stopBtn.style.display = 'none';
    showAlert('Re-Auth Massal dihentikan', false);
  }
}

async function handleReauthAll() {
  const box = $('#reauth-all-progress-box');
  if (!box) return;

  box.style.display = 'block';
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  if (!state.routers || state.routers.length === 0) {
    try {
      const data = await api('/routers');
      state.routers = data.routers || [];
    } catch (_) {}
  }

  const tbody = $('#reauth-all-tbody');
  const stopBtn = $('#btn-stop-reauth-all');
  const btnReauthAll = $('#btn-reauth-all');

  if (!state.routers || state.routers.length === 0) {
    $('#reauth-all-status-text').textContent = '⚠️ Belum ada router terdaftar di sistem.';
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="5" style="padding:1.5rem; text-align:center; color:var(--text-muted);">
        Belum ada router di database. Gunakan tombol <strong>+ Tambah Router</strong> atau <strong>Range IP / Subnet</strong> untuk mendaftarkan router terlebih dahulu.
      </td></tr>`;
    }
    return;
  }

  const totalRouters = state.routers.length;
  $('#reauth-all-bar').style.width = '0%';
  $('#reauth-all-status-text').textContent = `⏳ Memulai Re-Auth Massal (${totalRouters} router)...`;
  $('#reauth-all-badge').textContent = `0 / ${totalRouters} Router (0%)`;
  if ($('#reauth-stat-total')) $('#reauth-stat-total').textContent = `Total: ${totalRouters}`;
  if ($('#reauth-stat-processed')) $('#reauth-stat-processed').textContent = 'Diproses: 0';
  $('#reauth-stat-success').textContent = '✓ Berhasil: 0';
  if ($('#reauth-stat-ssid')) $('#reauth-stat-ssid').textContent = '📶 SSID Ditemukan: 0';
  $('#reauth-stat-offline').textContent = '✕ Offline: 0';
  $('#reauth-stat-failed').textContent = '⚠️ Gagal: 0';

  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="5" style="padding:1.5rem; text-align:center; color:var(--text-muted);">
      ⏳ Membuka koneksi paralel dan melakukan login ulang...
    </td></tr>`;
  }

  if (stopBtn) stopBtn.style.display = 'inline-block';
  if (btnReauthAll) btnReauthAll.disabled = true;

  reauthAllAbortController = new AbortController();
  const signal = reauthAllAbortController.signal;

  let successCount = 0;
  let ssidFoundCount = 0;
  let offlineCount = 0;
  let failedCount = 0;
  let processedCount = 0;
  let totalClientsCount = 0;
  let hasRows = false;
  let brandStats = {};
  let hostnameStats = {};
  let allCollectedClients = [];

  // Reset pie chart and analytics panel
  drawReauthPieChart({}, 0);
  if ($('#reauth-brand-legend-tbody')) {
    $('#reauth-brand-legend-tbody').innerHTML = '<tr><td colspan="3" style="padding:1rem; text-align:center; color:var(--text-muted);">Sedang menghitung client dari router...</td></tr>';
  }
  if ($('#reauth-hostnames-tbody')) {
    $('#reauth-hostnames-tbody').innerHTML = '<tr><td colspan="2" style="padding:1rem; text-align:center; color:var(--text-muted);">Sedang mendeteksi hostname...</td></tr>';
  }

  try {
    const resp = await fetch('/routers/reauth-all-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        concurrency: 10,
        timeout_sec: 6.0,
        fetch_ssid: true,
        fetch_clients: true,
      }),
      signal: signal,
    });

    if (!resp.ok) {
      let errDetail = 'Gagal menjalankan Re-Auth Massal';
      try {
        const errJson = await resp.json();
        errDetail = errJson.detail || errDetail;
      } catch (_) {}
      throw new Error(errDetail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const jsonStr = trimmed.slice(5).trim();
        if (!jsonStr) continue;

        try {
          const ev = JSON.parse(jsonStr);
          if (ev.type === 'start') {
            $('#reauth-all-status-text').textContent = `⏳ Memproses Re-Auth Massal (${ev.total} router, 10 worker paralel)...`;
            $('#reauth-all-badge').textContent = `0 / ${ev.total} Router (0%)`;
          } else if (ev.type === 'checking') {
            $('#reauth-all-status-text').textContent = `⏳ [${ev.index}/${ev.total}] Memeriksa ${esc(ev.ip)} (${esc(ev.name)})...`;
          } else if (ev.type === 'result') {
            processedCount++;
            const pct = ev.percent || (ev.total ? Math.round((processedCount / ev.total) * 100) : 100);
            $('#reauth-all-bar').style.width = `${pct}%`;
            $('#reauth-all-badge').textContent = `${processedCount} / ${ev.total} Router (${pct}%)`;

            const item = ev.item || {};
            if (item.status === 'ok') {
              successCount++;
              if (item.ssid) ssidFoundCount++;
              const clients = item.clients || [];
              totalClientsCount += clients.length;
              for (const c of clients) {
                allCollectedClients.push({
                  router_ip: item.ip,
                  router_name: item.name,
                  router_model: item.model,
                  ssid: item.ssid,
                  ...c
                });
                const b = c.brand || 'Tanpa Hostname';
                brandStats[b] = (brandStats[b] || 0) + 1;
                if (c.hostname) {
                  hostnameStats[c.hostname] = (hostnameStats[c.hostname] || 0) + 1;
                }
              }
            } else if (item.status === 'offline') {
              offlineCount++;
            } else {
              failedCount++;
            }

            if ($('#reauth-stat-processed')) $('#reauth-stat-processed').textContent = `Diproses: ${processedCount}`;
            $('#reauth-stat-success').textContent = `✓ Berhasil: ${successCount}`;
            if ($('#reauth-stat-ssid')) $('#reauth-stat-ssid').textContent = `📶 SSID: ${ssidFoundCount}`;
            if ($('#reauth-stat-clients')) $('#reauth-stat-clients').textContent = `📱 Perangkat Client: ${totalClientsCount}`;
            $('#reauth-stat-offline').textContent = `✕ Offline: ${offlineCount}`;
            $('#reauth-stat-failed').textContent = `⚠️ Gagal: ${failedCount}`;

            // Update pie chart and tables live every few items or when clients found
            if (item.client_count > 0 || processedCount % 5 === 0) {
              drawReauthPieChart(brandStats, totalClientsCount);
              renderReauthAnalyticsTables(brandStats, hostnameStats, totalClientsCount);
            }

            // Append row to live progress table
            if (tbody) {
              if (!hasRows) {
                tbody.innerHTML = '';
                hasRows = true;
              }

              const tr = document.createElement('tr');
              tr.style.borderBottom = '1px solid var(--border)';

              let statusBadge = '';
              if (item.status === 'ok') {
                statusBadge = '<span class="badge badge-online">✓ Berhasil</span>';
              } else if (item.status === 'offline') {
                statusBadge = '<span class="badge badge-offline">✕ Offline</span>';
              } else {
                statusBadge = `<span class="badge badge-warn">⚠️ ${esc(item.status)}</span>`;
              }

              const ssidHtml = item.ssid
                ? `<strong style="color:#6d28d9;">📶 ${esc(item.ssid)}</strong>`
                : '<span style="color:var(--text-muted); font-style:italic;">-</span>';

              const modelBadge = item.model
                ? `<span class="badge" style="background:#e0f2fe; color:#0369a1; font-size:0.75rem;">ZTE ${esc(item.model)}</span>`
                : '';

              const clientsBadge = item.client_count > 0
                ? `<span class="badge" style="background:#dbeafe; color:#1e40af; font-weight:700;">📱 ${item.client_count} device</span>`
                : `<span style="color:var(--text-muted); font-size:0.8rem;">0</span>`;

              const latHtml = item.latency_ms
                ? `<span style="font-family:monospace; color:var(--text-muted);">${item.latency_ms}ms</span>`
                : `<span style="color:var(--text-muted); font-size:0.8rem;">${esc(item.message || '-')}</span>`;

              tr.innerHTML = `
                <td style="padding:0.5rem 0.75rem; font-family:monospace; font-weight:700;">
                  <a href="http://${esc(item.ip)}:${item.port || 80}" target="_blank" rel="noopener" style="color:var(--primary); text-decoration:none;">
                    ${esc(item.ip)}${item.port && item.port !== 80 ? ':' + item.port : ''}
                  </a>
                </td>
                <td style="padding:0.5rem 0.75rem;">
                  <div>${esc(item.name)}</div>
                  <div style="margin-top:0.15rem;">${modelBadge}</div>
                </td>
                <td style="padding:0.5rem 0.75rem;">${statusBadge}</td>
                <td style="padding:0.5rem 0.75rem;">${ssidHtml}</td>
                <td style="padding:0.5rem 0.75rem;">${clientsBadge}</td>
                <td style="padding:0.5rem 0.75rem;">${latHtml}</td>
              `;
              tbody.appendChild(tr);

              // Auto-scroll table to bottom
              const tableContainer = tbody.closest('div');
              if (tableContainer) {
                tableContainer.scrollTop = tableContainer.scrollHeight;
              }
            }
          } else if (ev.type === 'done') {
            $('#reauth-all-status-text').textContent = `✅ ${esc(ev.message)}`;
            $('#reauth-all-bar').style.width = '100%';
            $('#reauth-all-badge').textContent = `${ev.total} / ${ev.total} Router (100%)`;
            if (ev.total_clients !== undefined) {
              totalClientsCount = ev.total_clients;
            }
            if (ev.client_analytics?.brands) {
              brandStats = ev.client_analytics.brands;
            }
            if (ev.client_analytics?.top_hostnames) {
              hostnameStats = ev.client_analytics.top_hostnames;
            }
            drawReauthPieChart(brandStats, totalClientsCount);
            renderReauthAnalyticsTables(brandStats, hostnameStats, totalClientsCount);
            showAlert(ev.message, false);
          }
        } catch (_) {}
      }
    }

    // Refresh router select dropdown so new SSID labels appear
    await loadRouters();
  } catch (err) {
    if (err.name === 'AbortError') {
      $('#reauth-all-status-text').textContent = '⏹️ Re-Auth Massal dihentikan oleh pengguna.';
    } else {
      $('#reauth-all-status-text').textContent = `❌ Terjadi kesalahan: ${esc(err.message)}`;
      showAlert(err.message, true);
    }
  } finally {
    if (stopBtn) stopBtn.style.display = 'none';
    if (btnReauthAll) btnReauthAll.disabled = false;
    reauthAllAbortController = null;
    state.collectedClients = allCollectedClients;
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

  // 6. Connected DHCP Clients
  renderClients(data.devices || []);

  // 7. Raw JSON
  $('#json-raw-content').textContent = JSON.stringify(data, null, 2);
}

function renderClients(clientList) {
  const tbody = $('#clients-tbody');
  const countBadge = $('#single-clients-count-badge');
  const count = clientList ? clientList.length : 0;
  if (countBadge) countBadge.textContent = `${count} Klien`;

  if (!clientList || clientList.length === 0) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:1.25rem;">Tidak ada perangkat terhubung (DHCP) terdeteksi pada router ini</td></tr>';
    return;
  }

  if (tbody) {
    tbody.innerHTML = clientList
      .map((c) => {
        const ip = c.ip_address || '-';
        const mac = c.mac_address || '-';
        const hn = c.hostname ? esc(c.hostname) : '<em style="color:var(--text-muted);">Tanpa Nama</em>';
        const iface = c.interface ? `<span class="badge badge-neutral">${esc(c.interface)}</span>` : '-';
        const lease = c.last_seen ? esc(c.last_seen) : '-';
        const brand = classifyDeviceBrand(c.hostname, c.mac_address);

        return `
          <tr>
            <td style="font-family:monospace; font-weight:700;">${esc(ip)}</td>
            <td style="font-family:monospace; color:var(--text-muted);">${esc(mac)}</td>
            <td><strong>${hn}</strong></td>
            <td><span class="badge" style="background:#e0f2fe; color:#0369a1; font-weight:600;">${esc(brand)}</span></td>
            <td>${iface}</td>
            <td style="color:var(--text-muted); font-size:0.85rem;">${lease}</td>
          </tr>
        `;
      })
      .join('');
  }
}

function classifyDeviceBrand(hostname, mac) {
  const hn = (hostname || '').toLowerCase().trim();
  if (hn.includes('oppo')) return 'OPPO';
  if (hn.includes('realme')) return 'Realme';
  if (['redmi', 'xiaomi', 'poco', 'mi '].some(k => hn.includes(k))) return 'Xiaomi / Redmi / POCO';
  if (['galaxy', 'samsung'].some(k => hn.includes(k))) return 'Samsung';
  if (hn.includes('vivo') || hn.includes('iqoo') || hn.startsWith('v2') || hn.startsWith('i2')) return 'Vivo / iQOO';
  if (['infinix', 'tecno', 'itel'].some(k => hn.includes(k))) return 'Infinix / Transsion';
  if (['iphone', 'ipad', 'apple', 'macbook'].some(k => hn.includes(k))) return 'Apple (iOS/macOS)';
  if (['desktop', 'laptop', 'windows', 'pc-'].some(k => hn.includes(k))) return 'PC / Laptop Windows';
  if (['tenda', 'tp-link', 'totolink', 'mercusys', 'router', 'cpe', 'ap-'].some(k => hn.includes(k))) return 'Router / Extender';
  if (['tv', 'android', 'stb', 'box'].some(k => hn.includes(k))) return 'Smart TV / Android Box';
  if (['aliosthings', 'tuya', 'esp_', 'esp32', 'esp8266', 'iot', 'cam', 'ipc-'].some(k => hn.includes(k))) return 'IoT / IP Cam / Smart Home';
  if (hn) return `Lainnya (${hostname})`;
  return 'Tanpa Hostname';
}

const PIE_COLORS = [
  '#2563eb', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#06b6d4', '#ec4899', '#f97316', '#14b8a6', '#6366f1',
  '#84cc16', '#a855f7', '#64748b', '#0284c7', '#d97706',
];

function drawReauthPieChart(brandData, totalCount) {
  const canvas = $('#reauth-pie-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);

  const totalBadge = $('#reauth-pie-total-badge');
  if (totalBadge) {
    totalBadge.textContent = `Total Client: ${totalCount} Perangkat`;
  }

  const entries = Object.entries(brandData || {}).filter(([_, count]) => count > 0);
  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(centerX, centerY) - 10;
  const innerRadius = radius * 0.52; // Donut style

  if (totalCount === 0 || entries.length === 0) {
    // Draw empty placeholder ring
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, 2 * Math.PI);
    ctx.arc(centerX, centerY, innerRadius, 2 * Math.PI, 0, true);
    ctx.fillStyle = '#e2e8f0';
    ctx.fill();

    ctx.fillStyle = '#94a3b8';
    ctx.font = 'bold 12px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('0 Client', centerX, centerY);
    return;
  }

  // Sort slices from largest to smallest
  entries.sort((a, b) => b[1] - a[1]);

  let startAngle = -0.5 * Math.PI; // Start at 12 o'clock
  entries.forEach(([brand, count], idx) => {
    const sliceAngle = (count / totalCount) * 2 * Math.PI;
    const endAngle = startAngle + sliceAngle;
    const color = PIE_COLORS[idx % PIE_COLORS.length];

    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, startAngle, endAngle);
    ctx.arc(centerX, centerY, innerRadius, endAngle, startAngle, true);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();

    startAngle = endAngle;
  });

  // Inner circle text
  ctx.fillStyle = '#0f172a';
  ctx.font = 'bold 18px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(`${totalCount}`, centerX, centerY - 8);

  ctx.fillStyle = '#64748b';
  ctx.font = '10px sans-serif';
  ctx.fillText('Perangkat', centerX, centerY + 12);
}

function renderReauthAnalyticsTables(brandData, hostData, totalCount) {
  const brandTbody = $('#reauth-brand-legend-tbody');
  const hostTbody = $('#reauth-hostnames-tbody');

  if (brandTbody) {
    const brandEntries = Object.entries(brandData || {}).filter(([_, count]) => count > 0);
    brandEntries.sort((a, b) => b[1] - a[1]);

    if (brandEntries.length === 0) {
      brandTbody.innerHTML = '<tr><td colspan="3" style="padding:1rem; text-align:center; color:var(--text-muted);">Belum ada data merek terhitung</td></tr>';
    } else {
      brandTbody.innerHTML = brandEntries
        .map(([brand, count], idx) => {
          const color = PIE_COLORS[idx % PIE_COLORS.length];
          const pct = totalCount ? Math.round((count / totalCount) * 100) : 0;
          return `
            <tr style="border-bottom:1px solid var(--border);">
              <td style="padding:0.4rem 0.6rem; width:20px;">
                <span style="display:inline-block; width:12px; height:12px; border-radius:3px; background:${color}; vertical-align:middle;"></span>
              </td>
              <td style="padding:0.4rem 0.6rem; font-weight:600; color:var(--text-main);">${esc(brand)}</td>
              <td style="padding:0.4rem 0.6rem; text-align:right;">
                <span style="font-weight:700;">${count}</span>
                <span style="color:var(--text-muted); font-size:0.78rem; margin-left:4px;">(${pct}%)</span>
              </td>
            </tr>
          `;
        })
        .join('');
    }
  }

  if (hostTbody) {
    const hostEntries = Object.entries(hostData || {}).filter(([_, count]) => count > 0);
    hostEntries.sort((a, b) => b[1] - a[1]);

    if (hostEntries.length === 0) {
      hostTbody.innerHTML = '<tr><td colspan="2" style="padding:1rem; text-align:center; color:var(--text-muted);">Belum ada hostname terdeteksi</td></tr>';
    } else {
      hostTbody.innerHTML = hostEntries.slice(0, 25)
        .map(([hostname, count]) => {
          return `
            <tr style="border-bottom:1px solid var(--border);">
              <td style="padding:0.4rem 0.6rem; font-weight:600; font-family:monospace; color:var(--text-main);">${esc(hostname)}</td>
              <td style="padding:0.4rem 0.6rem; text-align:right;">
                <span class="badge" style="background:#e0f2fe; color:#0369a1; font-weight:700;">${count} unit</span>
              </td>
            </tr>
          `;
        })
        .join('');
    }
  }
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
  $('#form-timeout').value = '10';
  $('#form-location').value = '';
  $('#form-notes').value = '';

  // Reset single router credential mode to auto (F609 by default)
  if ($('#single-cred-auto')) $('#single-cred-auto').checked = true;
  if ($('#single-cred-auto-box')) $('#single-cred-auto-box').style.display = 'block';
  if ($('#single-cred-manual-box')) $('#single-cred-manual-box').style.display = 'none';
  if ($('#preset-f609')) $('#preset-f609').checked = true;
  $('#form-username').value = 'admin';
  $('#form-password').value = 'admin';

  // Mode tabs visible during Add
  if ($('#router-mode-tabs')) {
    $('#router-mode-tabs').style.display = 'flex';
  }
  switchToSingleMode();

  // Reset range form fields
  $('#range-cidr').value = '';
  $('#range-port').value = '80';
  $('#range-username').value = 'admin';
  $('#range-password').value = '';
  $('#range-timeout').value = '2';
  $('#range-location').value = '';
  $('#range-notes').value = '';
  $('#range-filter-f609').checked = true;
  $('#range-filter-f677v2').checked = true;
  // Reset credential mode to auto
  if ($('#range-cred-auto')) $('#range-cred-auto').checked = true;
  if ($('#range-cred-auto-info')) $('#range-cred-auto-info').style.display = 'block';
  if ($('#range-cred-manual-fields')) $('#range-cred-manual-fields').style.display = 'none';
  $('#range-scan-box').style.display = 'none';

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

  // In edit mode, show manual credential inputs directly
  if ($('#single-cred-manual')) $('#single-cred-manual').checked = true;
  if ($('#single-cred-auto-box')) $('#single-cred-auto-box').style.display = 'none';
  if ($('#single-cred-manual-box')) $('#single-cred-manual-box').style.display = 'block';

  // In edit mode, hide range tab
  if ($('#router-mode-tabs')) {
    $('#router-mode-tabs').style.display = 'none';
  }
  switchToSingleMode();
  $('#range-scan-box').style.display = 'none';

  $('#router-form-card').style.display = 'block';
  $('#router-form-card').scrollIntoView({ behavior: 'smooth' });
}

function closeForm() {
  $('#router-form-card').style.display = 'none';
  $('#range-scan-box').style.display = 'none';
}

function switchToSingleMode() {
  $('#tab-mode-single').classList.add('btn-primary');
  $('#tab-mode-range').classList.remove('btn-primary');
  $('#router-form').style.display = 'block';
  $('#router-range-form').style.display = 'none';
}

function switchToRangeMode() {
  $('#tab-mode-range').classList.add('btn-primary');
  $('#tab-mode-single').classList.remove('btn-primary');
  $('#router-form').style.display = 'none';
  $('#router-range-form').style.display = 'block';
}

async function handleRangeScanSubmit(e) {
  e.preventDefault();
  const cidr = $('#range-cidr').value.trim();
  const port = parseInt($('#range-port').value, 10) || 80;
  const credentialMode = document.querySelector('input[name="range-cred-mode"]:checked')?.value || 'auto';
  const username = credentialMode === 'manual' ? ($('#range-username').value.trim() || 'admin') : 'admin';
  const password = credentialMode === 'manual' ? ($('#range-password').value || '') : '';
  const connectTimeout = parseFloat($('#range-timeout').value) || 2.0;
  const nameTpl = $('#range-name-tpl').value.trim() || 'ZTE {model} ({ip})';
  const location = $('#range-location').value.trim();
  const notes = $('#range-notes').value.trim();

  const allowed = [];
  if ($('#range-filter-f609')?.checked) allowed.push('F609');
  if ($('#range-filter-f677v2')?.checked) allowed.push('F677V2');
  if ($('#range-filter-f477v2')?.checked) allowed.push('F477V2');

  if (!cidr) {
    showAlert('Range IP / CIDR Subnet wajib diisi (misal: 10.200.204.0/24)', true);
    return;
  }

  if (allowed.length === 0) {
    showAlert('Pilih minimal salah satu model (F609, F677V2, atau F477V2)', true);
    return;
  }

  const btn = $('#btn-start-range-scan');
  btn.disabled = true;
  btn.textContent = '⏳ Memindai subnet...';

  // Prepare Scan Progress UI
  $('#range-scan-box').style.display = 'block';
  $('#range-scan-status').textContent = `⏳ Memindai ${cidr}...`;
  $('#range-scan-badge').textContent = '0 / ? IP (0%)';
  $('#range-scan-bar').style.width = '0%';
  $('#scan-stat-scanned').textContent = 'Dipindai: 0';
  $('#scan-stat-found').textContent = 'Ditemukan: 0';
  $('#scan-stat-added').textContent = 'Ditambahkan: 0';
  $('#scan-stat-exists').textContent = 'Sudah Ada: 0';

  const tbody = $('#range-scan-tbody');
  tbody.innerHTML = `<tr><td colspan="5" style="padding:1rem; text-align:center; color:var(--text-muted);">Sedang memindai alamat IP aktif dan memeriksa tipe ZTE F609 / F677V2 / F477V2...</td></tr>`;

  let foundCount = 0;
  let addedCount = 0;
  let existsCount = 0;
  let hasRows = false;

  try {
    const resp = await fetch('/routers/scan-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cidr: cidr,
        port: port,
        credential_mode: credentialMode,
        username: username,
        password: password,
        timeout: 10,
        connect_timeout: connectTimeout,
        allowed_models: allowed,
        name_template: nameTpl,
        location: location,
        notes: notes,
        auto_add: true,
        concurrency: 60,
      }),
    });

    if (!resp.ok) {
      let errDetail = 'Gagal memindai subnet';
      try {
        const errJson = await resp.json();
        errDetail = errJson.detail || errDetail;
      } catch (_) {}
      throw new Error(errDetail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep last incomplete chunk

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const jsonStr = trimmed.slice(5).trim();
        if (!jsonStr) continue;

        try {
          const ev = JSON.parse(jsonStr);
          if (ev.type === 'start') {
            $('#range-scan-status').textContent = `⏳ Memindai ${ev.total} IP (${cidr})...`;
            $('#range-scan-badge').textContent = `0 / ${ev.total} IP (0%)`;
          } else if (ev.type === 'progress') {
            $('#range-scan-bar').style.width = `${ev.percent}%`;
            $('#range-scan-badge').textContent = `${ev.scanned} / ${ev.total} IP (${ev.percent}%)`;
            $('#scan-stat-scanned').textContent = `Dipindai: ${ev.scanned}`;
          } else if (ev.type === 'found') {
            const item = ev.item;
            foundCount++;
            if (item.status === 'added') addedCount++;
            if (item.status === 'already_exists') existsCount++;

            $('#scan-stat-found').textContent = `Ditemukan: ${foundCount}`;
            $('#scan-stat-added').textContent = `Ditambahkan: ${addedCount}`;
            $('#scan-stat-exists').textContent = `Sudah Ada: ${existsCount}`;

            if (!hasRows) {
              tbody.innerHTML = '';
              hasRows = true;
            }

            const statusBadge = item.status === 'added'
              ? `<span class="badge badge-online">Ditambahkan</span>`
              : (item.status === 'already_exists'
                 ? `<span class="badge badge-warn">Sudah Ada</span>`
                 : `<span class="badge badge-offline">Gagal</span>`);

            const modelColor = item.model.includes('677') ? '#15803d' : '#0369a1';
            const modelBg = item.model.includes('677') ? '#dcfce7' : '#e0f2fe';
            const modelBadge = `<span class="badge" style="font-weight:700; background:${modelBg}; color:${modelColor};">ZTE ${esc(item.model)}</span>`;

            const row = document.createElement('tr');
            row.style.borderBottom = '1px solid var(--border)';
            const userDisplay = item.username
              ? `<code style="font-size:0.8rem;">${esc(item.username)}</code>`
              : `<span style="color:var(--text-muted);">-</span>`;
            row.innerHTML = `
              <td style="padding:0.4rem 0.6rem; font-family:monospace; font-weight:600;">${esc(item.ip)}</td>
              <td style="padding:0.4rem 0.6rem;">${modelBadge}</td>
              <td style="padding:0.4rem 0.6rem;">${esc(item.name)}</td>
              <td style="padding:0.4rem 0.6rem;">${userDisplay}</td>
              <td style="padding:0.4rem 0.6rem;">${statusBadge}</td>
            `;
            tbody.appendChild(row);
          } else if (ev.type === 'done') {
            $('#range-scan-bar').style.width = '100%';
            $('#range-scan-badge').textContent = `${ev.total_scanned} / ${ev.total_scanned} IP (100%)`;
            $('#range-scan-status').innerHTML = `✅ <strong>Selesai!</strong> Ditemukan <strong>${ev.found_count}</strong> router (${ev.added_count} baru ditambahkan).`;
            showAlert(ev.message);
            await loadRouters();
          } else if (ev.type === 'error') {
            throw new Error(ev.message);
          }
        } catch (parseErr) {
          console.error('Error parsing SSE event:', parseErr);
        }
      }
    }
  } catch (err) {
    showAlert(`Pemindaian gagal: ${err.message}`, true);
    $('#range-scan-status').innerHTML = `❌ <span style="color:var(--danger-text)">Gagal: ${esc(err.message)}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Scan & Tambah Router';
  }
}

async function handleFormSubmit(e) {
  e.preventDefault();
  const id = $('#form-id').value.trim();
  const isAuto = !id && document.querySelector('input[name="single-cred-mode"]:checked')?.value === 'auto';
  const autoPreset = isAuto ? (document.querySelector('input[name="single-auto-preset"]:checked')?.value || 'F609') : null;

  let username = $('#form-username').value.trim() || 'admin';
  let password = $('#form-password').value;

  if (isAuto) {
    if (autoPreset === 'F609') {
      username = 'admin';
      password = 'admin';
    } else if (autoPreset === 'F677V2') {
      username = 'superadmin';
      password = 'suportadmin';
    } else if (autoPreset === 'F477V2') {
      username = 'Administrator';
      password = 'suportadmin';
    }
  }

  const payload = {
    name: $('#form-name').value.trim(),
    host: $('#form-host').value.trim(),
    port: parseInt($('#form-port').value, 10) || 80,
    username: username,
    password: password,
    timeout: parseInt($('#form-timeout').value, 10) || 10,
    location: $('#form-location').value.trim(),
    notes: $('#form-notes').value.trim(),
  };

  if (!id && isAuto) {
    payload.credential_mode = 'auto';
    payload.model = autoPreset;
  }

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

// ==============================================================================
// Multi-Router SSID Search (IP Locator with per-router Re-Auth)
// ==============================================================================

let ssidSearchAbortController = null;
let lastSsidSearchResults = [];

function toggleSsidSearchPanel() {
  const card = $('#ssid-search-card');
  if (!card) return;
  if (card.style.display === 'none' || !card.style.display) {
    card.style.display = 'block';
    $('#search-ssid-query')?.focus();
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else {
    card.style.display = 'none';
  }
}

function closeSsidSearchPanel() {
  if (ssidSearchAbortController) {
    ssidSearchAbortController.abort();
    ssidSearchAbortController = null;
  }
  const card = $('#ssid-search-card');
  if (card) card.style.display = 'none';
}

function stopSsidSearch() {
  if (ssidSearchAbortController) {
    ssidSearchAbortController.abort();
    ssidSearchAbortController = null;
    $('#ssid-search-status-text').textContent = '⏹️ Pencarian dihentikan oleh pengguna.';
    $('#btn-start-ssid-search').style.display = 'inline-block';
    $('#btn-stop-ssid-search').style.display = 'none';
    $('#search-ssid-query').disabled = false;
  }
}

async function handleSsidSearchSubmit(e) {
  e.preventDefault();
  const query = $('#search-ssid-query').value.trim();
  const matchMode = document.querySelector('input[name="search-ssid-match-mode"]:checked')?.value || 'contains';
  const isExact = matchMode === 'exact';
  const reauth = $('#search-ssid-reauth').checked;
  const concurrency = parseInt($('#search-ssid-concurrency').value, 10) || 5;
  const timeoutSec = parseFloat($('#search-ssid-timeout').value) || 6.0;

  lastSsidSearchResults = [];
  const exportBtn = $('#btn-export-search-results-csv');
  if (exportBtn) exportBtn.style.display = 'none';

  // Setup UI
  $('#ssid-search-progress-box').style.display = 'block';
  $('#ssid-search-bar').style.width = '0%';
  $('#ssid-search-status-text').textContent = '⏳ Memulai pencarian SSID & re-auth...';
  $('#ssid-search-badge').textContent = '0 / 0 Router (0%)';
  $('#ssid-stat-scanned').textContent = 'Diperiksa: 0';
  $('#ssid-stat-matched').textContent = 'Ditemukan: 0';
  $('#ssid-stat-offline').textContent = 'Offline: 0';
  $('#ssid-stat-error').textContent = 'Gagal/Auth: 0';

  const tbody = $('#ssid-search-tbody');
  tbody.innerHTML = `<tr><td colspan="6" style="padding:1.5rem; text-align:center; color:var(--text-muted);">
    ⏳ Menghubungi router dan memeriksa konfigurasi WiFi...
  </td></tr>`;

  $('#btn-start-ssid-search').style.display = 'none';
  $('#btn-stop-ssid-search').style.display = 'inline-block';
  $('#search-ssid-query').disabled = true;

  ssidSearchAbortController = new AbortController();
  const signal = ssidSearchAbortController.signal;

  let scannedCount = 0;
  let matchedCount = 0;
  let offlineCount = 0;
  let errorCount = 0;
  let hasMatchRows = false;

  try {
    const resp = await fetch('/routers/search-ssid-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: query,
        exact_match: isExact,
        reauth: reauth,
        concurrency: concurrency,
        timeout_sec: timeoutSec,
      }),
      signal: signal,
    });

    if (!resp.ok) {
      let errDetail = 'Gagal melakukan pencarian SSID';
      try {
        const errJson = await resp.json();
        errDetail = errJson.detail || errDetail;
      } catch (_) {}
      throw new Error(errDetail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const jsonStr = trimmed.slice(5).trim();
        if (!jsonStr) continue;

        try {
          const ev = JSON.parse(jsonStr);
          if (ev.type === 'start') {
            $('#ssid-search-status-text').textContent = `⏳ Memeriksa ${ev.total_routers} router untuk SSID "${esc(ev.query)}" (${ev.concurrency} worker)...`;
            $('#ssid-search-badge').textContent = `0 / ${ev.total_routers} Router (0%)`;
          } else if (ev.type === 'router_checking') {
            $('#ssid-search-status-text').textContent = `⏳ Memeriksa ${esc(ev.ip)} (${esc(ev.name)}) [${ev.index}/${ev.total}]...`;
          } else if (ev.type === 'router_result') {
            scannedCount++;
            const pct = ev.percent || (ev.total ? Math.round((scannedCount / ev.total) * 100) : 100);
            $('#ssid-search-bar').style.width = `${pct}%`;
            $('#ssid-search-badge').textContent = `${scannedCount} / ${ev.total} Router (${pct}%)`;
            $('#ssid-stat-scanned').textContent = `Diperiksa: ${scannedCount}`;

            const res = ev.result || {};
            if (res.status === 'matched') {
              matchedCount++;
              $('#ssid-stat-matched').textContent = `Ditemukan: ${matchedCount}`;
              lastSsidSearchResults.push(res);

              if (!hasMatchRows) {
                tbody.innerHTML = '';
                hasMatchRows = true;
              }

              const matchedSSIDs = res.matched_ssids || [];
              matchedSSIDs.forEach((net) => {
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid var(--border)';
                const modelBadge = res.model ? `<span class="badge" style="background:#e0f2fe; color:#0369a1; font-weight:600;">ZTE ${esc(res.model)}</span>` : '';
                const pwd = net.password || '';
                const statusBadge = net.enabled
                  ? `<span class="badge badge-online">Aktif</span>`
                  : `<span class="badge badge-offline">Nonaktif</span>`;
                const secBadge = `<span class="badge badge-neutral">${esc(net.security || 'WPA2-PSK')}</span>`;
                const bandBadge = net.band ? `<span class="badge badge-neutral" style="font-size:0.72rem;">${esc(net.band)}</span>` : '';

                tr.innerHTML = `
                  <td style="padding:0.65rem 0.75rem; font-family:monospace; font-weight:700; color:var(--primary);">
                    <a href="http://${esc(res.host)}:${res.port}" target="_blank" rel="noopener" style="color:inherit; text-decoration:none;" title="Buka web admin router">
                      ${esc(res.host)}${res.port !== 80 ? ':' + res.port : ''} ↗
                    </a>
                  </td>
                  <td style="padding:0.65rem 0.75rem;">
                    <div><strong>${esc(res.name)}</strong></div>
                    <div style="margin-top:0.2rem;">${modelBadge}</div>
                  </td>
                  <td style="padding:0.65rem 0.75rem;">
                    <div style="font-weight:700; color:#7c3aed; font-size:0.95rem;">${esc(net.ssid)}</div>
                    <div style="margin-top:0.2rem;">${bandBadge}</div>
                  </td>
                  <td style="padding:0.65rem 0.75rem;">
                    ${pwd ? `
                      <div style="display:flex; align-items:center; gap:0.4rem;">
                        <span class="pwd-val" data-plain="${esc(pwd)}" style="font-family:monospace; font-weight:600; letter-spacing:1px;">••••••••</span>
                        <button type="button" class="btn btn-sm btn-outline btn-toggle-pwd" title="Lihat Password" style="padding:0.15rem 0.4rem; font-size:0.75rem;">👁️</button>
                        <button type="button" class="btn btn-sm btn-outline btn-copy-pwd" data-pwd="${esc(pwd)}" title="Salin Password" style="padding:0.15rem 0.4rem; font-size:0.75rem;">📋 Salin</button>
                      </div>
                    ` : `<span style="color:var(--text-muted); font-style:italic;">(Tanpa Password / Open)</span>`}
                  </td>
                  <td style="padding:0.65rem 0.75rem;">
                    <div style="display:flex; gap:0.35rem; align-items:center; flex-wrap:wrap;">
                      ${statusBadge}
                      ${secBadge}
                    </div>
                  </td>
                  <td style="padding:0.65rem 0.75rem; text-align:right;">
                    <button type="button" class="btn btn-sm btn-primary btn-jump-router" data-id="${esc(res.router_id)}" style="font-size:0.78rem; padding:0.25rem 0.6rem;">
                      Buka di Panel
                    </button>
                  </td>
                `;
                tbody.appendChild(tr);
              });
            } else if (res.status === 'offline') {
              offlineCount++;
              $('#ssid-stat-offline').textContent = `Offline: ${offlineCount}`;
            } else if (res.status === 'auth_error' || res.status === 'error' || res.status === 'timeout') {
              errorCount++;
              $('#ssid-stat-error').textContent = `Gagal/Auth: ${errorCount}`;
            }
          } else if (ev.type === 'done') {
            $('#ssid-search-status-text').textContent = `✅ ${esc(ev.message)}`;
            $('#ssid-search-bar').style.width = '100%';
            $('#ssid-search-badge').textContent = `${ev.total_scanned} / ${ev.total_scanned} Router (100%)`;
            if (matchedCount > 0 && exportBtn) {
              exportBtn.style.display = 'inline-block';
            }
            if (!hasMatchRows) {
              tbody.innerHTML = `<tr><td colspan="6" style="padding:1.5rem; text-align:center; color:var(--text-muted);">
                Tidak ditemukan router dengan SSID "${esc(query)}" pada ${ev.total_scanned} router yang diperiksa.
              </td></tr>`;
            }
          } else if (ev.type === 'error') {
            $('#ssid-search-status-text').textContent = `⚠️ Error: ${esc(ev.message)}`;
          }
        } catch (_) {}
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      $('#ssid-search-status-text').textContent = '⏹️ Pencarian dihentikan oleh pengguna.';
    } else {
      $('#ssid-search-status-text').textContent = `❌ Terjadi kesalahan: ${esc(err.message)}`;
      showAlert(err.message, true);
    }
  } finally {
    $('#btn-start-ssid-search').style.display = 'inline-block';
    $('#btn-stop-ssid-search').style.display = 'none';
    $('#search-ssid-query').disabled = false;
    ssidSearchAbortController = null;
  }
}

function exportSearchResultsCsv() {
  if (!lastSsidSearchResults || !lastSsidSearchResults.length) {
    showAlert('Tidak ada data hasil pencarian untuk diekspor', true);
    return;
  }
  let csvContent = 'ip,port,router_name,model,ssid,password,enabled,security\n';
  lastSsidSearchResults.forEach((res) => {
    (res.matched_ssids || []).forEach((net) => {
      const row = [
        `"${(res.host || '').replace(/"/g, '""')}"`,
        res.port || 80,
        `"${(res.name || '').replace(/"/g, '""')}"`,
        `"${(res.model || '').replace(/"/g, '""')}"`,
        `"${(net.ssid || '').replace(/"/g, '""')}"`,
        `"${(net.password || '').replace(/"/g, '""')}"`,
        net.enabled ? '1' : '0',
        `"${(net.security || '').replace(/"/g, '""')}"`,
      ];
      csvContent += row.join(',') + '\n';
    });
  });

  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `ssid_search_results_${Date.now()}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Event Bindings
function initEvents() {
  // Router Dropdown Search / Filter Input
  const routerFilterInput = $('#router-filter-input');
  if (routerFilterInput) {
    routerFilterInput.addEventListener('input', (e) => {
      const q = e.target.value;
      const filtered = populateRouterSelect(q);
      const select = $('#router-select');
      if (filtered.length > 0) {
        if (!filtered.some((r) => r.id === state.selectedId)) {
          state.selectedId = filtered[0].id;
          select.value = state.selectedId;
          localStorage.setItem('zte_selected_router', state.selectedId);
          $('#delete-confirm-box').style.display = 'none';
          closeForm();
          refreshRouterData();
          testRouterConnection(state.selectedId);
        } else {
          select.value = state.selectedId;
        }
      }
    });

    routerFilterInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        routerFilterInput.value = '';
        populateRouterSelect('');
        if (state.selectedId) $('#router-select').value = state.selectedId;
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const select = $('#router-select');
        if (select.value && select.value !== state.selectedId) {
          state.selectedId = select.value;
          localStorage.setItem('zte_selected_router', state.selectedId);
          $('#delete-confirm-box').style.display = 'none';
          closeForm();
          refreshRouterData();
          testRouterConnection(state.selectedId);
        }
      }
    });
  }

  const btnClearRouterFilter = $('#btn-clear-router-filter');
  if (btnClearRouterFilter) {
    btnClearRouterFilter.addEventListener('click', () => {
      if (routerFilterInput) {
        routerFilterInput.value = '';
        routerFilterInput.focus();
      }
      populateRouterSelect('');
      if (state.selectedId) $('#router-select').value = state.selectedId;
    });
  }

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

  $('#btn-reauth-all')?.addEventListener('click', handleReauthAll);
  $('#btn-stop-reauth-all')?.addEventListener('click', stopReauthAll);
  $('#btn-close-reauth-all')?.addEventListener('click', closeReauthAll);

  $('#btn-refresh').addEventListener('click', () => {
    refreshRouterData();
    testRouterConnection(state.selectedId);
  });

  $('#refresh-interval').addEventListener('change', setRefreshInterval);

  $('#btn-toggle-add').addEventListener('click', openAddForm);
  $('#btn-edit-router').addEventListener('click', openEditForm);
  $('#btn-cancel-form').addEventListener('click', closeForm);
  $('#router-form').addEventListener('submit', handleFormSubmit);

  // Range scan listeners
  $('#tab-mode-single').addEventListener('click', switchToSingleMode);
  $('#tab-mode-range').addEventListener('click', switchToRangeMode);
  $('#router-range-form').addEventListener('submit', handleRangeScanSubmit);
  $('#btn-cancel-range-form').addEventListener('click', closeForm);

  // Single router credential mode toggle
  $$('input[name="single-cred-mode"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      const isManual = document.querySelector('input[name="single-cred-mode"]:checked')?.value === 'manual';
      if ($('#single-cred-auto-box')) $('#single-cred-auto-box').style.display = isManual ? 'none' : 'block';
      if ($('#single-cred-manual-box')) $('#single-cred-manual-box').style.display = isManual ? 'block' : 'none';
      if (!isManual) {
        updateSingleAutoCredentials();
      }
    });
  });

  $$('input[name="single-auto-preset"]').forEach((radio) => {
    radio.addEventListener('change', updateSingleAutoCredentials);
  });

  function updateSingleAutoCredentials() {
    const preset = document.querySelector('input[name="single-auto-preset"]:checked')?.value || 'F609';
    if (preset === 'F609') {
      $('#form-username').value = 'admin';
      $('#form-password').value = 'admin';
    } else if (preset === 'F677V2') {
      $('#form-username').value = 'superadmin';
      $('#form-password').value = 'suportadmin';
    } else if (preset === 'F477V2') {
      $('#form-username').value = 'Administrator';
      $('#form-password').value = 'suportadmin';
    } else {
      $('#form-username').value = 'admin';
      $('#form-password').value = '';
    }
  }

  // Credential mode radio toggle (range scan)
  $$('input[name="range-cred-mode"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      const isManual = document.querySelector('input[name="range-cred-mode"]:checked')?.value === 'manual';
      $('#range-cred-auto-info').style.display = isManual ? 'none' : 'block';
      $('#range-cred-manual-fields').style.display = isManual ? 'block' : 'none';
    });
  });

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

  // Backup & Decrypt Config handlers
  $('#btn-download-config')?.addEventListener('click', handleDownloadConfig);
  $('#btn-decrypt-router-config')?.addEventListener('click', handleDecryptRouterConfig);
  $('#btn-decrypt-upload')?.addEventListener('click', handleDecryptUpload);

  // Telnet Handlers
  $('#btn-check-telnet')?.addEventListener('click', handleCheckTelnet);
  $('#btn-enable-factory-telnet')?.addEventListener('click', handleEnableFactoryTelnet);
  $('#btn-extract-telnet')?.addEventListener('click', handleExtractTelnet);
  $('#btn-run-telnet-cmd')?.addEventListener('click', handleRunTelnetCmd);
  $$('.telnet-preset-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      $('#telnet-cmd-input').value = btn.dataset.cmd;
    });
  });

  // Backup CSV (IP, SSID, Password) handlers
  $('#btn-backup-all-csv')?.addEventListener('click', handleBackupCsvAll);
  $('#btn-export-all-wifi-csv')?.addEventListener('click', handleBackupCsvAll);
  $('#btn-export-single-wifi-csv')?.addEventListener('click', handleBackupCsvSingle);

  // Client Analytics & Export Handlers
  $('#btn-toggle-reauth-analytics')?.addEventListener('click', () => {
    const panel = $('#reauth-analytics-panel');
    if (!panel) return;
    const isHidden = panel.style.display === 'none';
    panel.style.display = isHidden ? 'block' : 'none';
    $('#btn-toggle-reauth-analytics').textContent = isHidden
      ? '📊 Sembunyikan Laporan Analitik'
      : '📊 Tampilkan Laporan Analitik Client';
  });

  $('#btn-export-clients-csv')?.addEventListener('click', exportAllClientsCsv);
  $('#btn-refresh-clients')?.addEventListener('click', () => {
    if (state.selectedId) {
      refreshRouterData();
    }
  });

  // SSID Search listeners
  $('#btn-toggle-search-ssid')?.addEventListener('click', toggleSsidSearchPanel);
  $('#btn-close-ssid-search')?.addEventListener('click', closeSsidSearchPanel);
  $('#ssid-search-form')?.addEventListener('submit', handleSsidSearchSubmit);
  $('#btn-stop-ssid-search')?.addEventListener('click', stopSsidSearch);
  $('#btn-export-search-results-csv')?.addEventListener('click', exportSearchResultsCsv);

  // Table action delegation for SSID search results
  $('#ssid-search-tbody')?.addEventListener('click', (e) => {
    // Copy password
    const copyBtn = e.target.closest('.btn-copy-pwd');
    if (copyBtn) {
      const pwd = copyBtn.dataset.pwd;
      if (pwd) {
        navigator.clipboard.writeText(pwd).then(() => {
          const oldText = copyBtn.textContent;
          copyBtn.textContent = '✓ Disalin!';
          copyBtn.style.color = '#15803d';
          setTimeout(() => {
            copyBtn.textContent = oldText;
            copyBtn.style.color = '';
          }, 1500);
        }).catch(() => {
          showAlert('Gagal menyalin password ke clipboard', true);
        });
      }
      return;
    }

    // Toggle password
    const toggleBtn = e.target.closest('.btn-toggle-pwd');
    if (toggleBtn) {
      const container = toggleBtn.closest('td');
      const valSpan = container?.querySelector('.pwd-val');
      if (valSpan) {
        if (valSpan.textContent === '••••••••') {
          valSpan.textContent = valSpan.dataset.plain;
          toggleBtn.textContent = '🙈';
        } else {
          valSpan.textContent = '••••••••';
          toggleBtn.textContent = '👁️';
        }
      }
      return;
    }

    // Jump to router
    const jumpBtn = e.target.closest('.btn-jump-router');
    if (jumpBtn) {
      const routerId = jumpBtn.dataset.id;
      if (routerId) {
        state.selectedId = routerId;
        const sel = $('#router-select');
        const filterInput = $('#router-filter-input');
        if (sel && !Array.from(sel.options).some(o => o.value === routerId)) {
          if (filterInput) filterInput.value = '';
          populateRouterSelect('');
        }
        if (sel) sel.value = routerId;
        localStorage.setItem('zte_selected_router', routerId);
        refreshRouterData();
        testRouterConnection(routerId);
        $('#main-content').scrollIntoView({ behavior: 'smooth', block: 'start' });
        showAlert(`Menampilkan router ${routerId}`);
      }
      return;
    }
  });
}

// Handler: Download config.bin
async function handleDownloadConfig() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  showAlert('Mengunduh config.bin dari router...', false);
  try {
    const res = await fetch(`/routers/${state.selectedId}/backup-config`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Gagal mengunduh backup');
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `config_${state.selectedId}.bin`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
    showAlert('✅ Berkas config.bin berhasil diunduh!', false);
  } catch (err) {
    showAlert('Gagal mengunduh backup: ' + err.message, true);
  }
}

// Handlers: Backup CSV (IP, SSID, Password)
async function handleBackupCsvAll() {
  showAlert('⏳ Memproses backup CSV seluruh router (IP, SSID, Password)... Mohon tunggu beberapa saat.', false);
  try {
    const res = await fetch('/routers/backup-csv');
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Gagal membuat backup CSV');
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `routers_wifi_backup_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
    showAlert('✅ Berkas CSV (IP, SSID, Password) seluruh router berhasil diunduh!', false);
  } catch (err) {
    showAlert('Gagal mengunduh CSV backup: ' + err.message, true);
  }
}

async function handleBackupCsvSingle() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const cur = state.routers.find((r) => r.id === state.selectedId);
  const routerHost = cur ? cur.host : state.selectedId;
  showAlert(`⏳ Mengunduh backup CSV WiFi untuk router ${routerHost}...`, false);
  try {
    const res = await fetch(`/routers/backup-csv?router_id=${state.selectedId}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Gagal membuat backup CSV');
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `router_${routerHost.replace(/\./g, '_')}_wifi_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
    showAlert('✅ Berkas CSV (IP, SSID, Password) router ini berhasil diunduh!', false);
  } catch (err) {
    showAlert('Gagal mengunduh CSV backup: ' + err.message, true);
  }
}

// Handler: Decrypt config directly from router
async function handleDecryptRouterConfig() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const btn = $('#btn-decrypt-router-config');
  btn.disabled = true;
  btn.textContent = '⏳ Mengunduh & Mendekripsi...';
  showAlert('Mengunduh dan mendekripsi config.bin...', false);

  try {
    const res = await fetch(`/routers/${state.selectedId}/decrypt-config`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Gagal mendekripsi');
    }
    renderDecryptedResults(data);
    showAlert('✅ Dekripsi berhasil! Kunci: ' + (data.encryption_key || 'Default'), false);
  } catch (err) {
    showAlert('Gagal mendekripsi: ' + err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>🔓</span> Dekripsi Otomatis dari Router';
  }
}

// Handler: Decrypt uploaded local config.bin
async function handleDecryptUpload() {
  const fileInput = $('#upload-config-file');
  if (!fileInput.files || fileInput.files.length === 0) {
    showAlert('Pilih file config.bin terlebih dahulu', true);
    return;
  }
  const file = fileInput.files[0];
  const customKey = ($('#upload-config-key').value || '').trim();

  const formData = new FormData();
  formData.append('file', file);

  const btn = $('#btn-decrypt-upload');
  btn.disabled = true;
  btn.textContent = '⏳ Mendekripsi...';

  try {
    const url = customKey ? `/decrypt-config?key=${encodeURIComponent(customKey)}` : '/decrypt-config';
    const res = await fetch(url, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Gagal mendekripsi file');
    }
    renderDecryptedResults(data);
    showAlert('✅ Dekripsi berkas lokal berhasil!', false);
  } catch (err) {
    showAlert('Gagal mendekripsi: ' + err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Dekripsi Berkas';
  }
}

// Render Decryption Results Box
function renderDecryptedResults(data) {
  const box = $('#decrypt-result-box');
  const container = $('#decrypt-credentials-container');
  box.style.display = 'block';

  const creds = data.credentials || {};
  const pppoes = creds.pppoe || [];
  const wifis = creds.wifi || [];
  const admins = creds.admin || [];

  let html = `
    <div style="background:var(--bg-card); border:1px solid var(--primary); border-radius:8px; padding:1.25rem; margin-bottom:1rem;">
      <div style="display:flex; justify-content:space-between; margin-bottom:1rem;">
        <div><strong>Status:</strong> <span class="badge badge-online">Sukses Terdekripsi</span></div>
        <div><strong>Kunci AES:</strong> <code>${esc(data.encryption_key || '-')}</code></div>
      </div>
  `;

  // PPPoE Credentials
  html += `<h5 style="margin:1rem 0 0.5rem 0; color:var(--primary);">🌐 Kredensial PPPoE (${pppoes.length})</h5>`;
  if (pppoes.length > 0) {
    html += `
      <table class="kv-table" style="margin-bottom:1rem;">
        <thead><tr><th>Username</th><th>Password (Plaintext)</th><th>Aksi</th></tr></thead>
        <tbody>
    `;
    pppoes.forEach((p, idx) => {
      html += `
        <tr>
          <td><code>${esc(p.username)}</code></td>
          <td><code style="color:var(--accent); font-weight:700;">${esc(p.password)}</code></td>
          <td>
            <button class="btn btn-sm btn-outline" onclick="navigator.clipboard.writeText('${esc(p.password)}'); this.textContent='✓ Disalin'; setTimeout(()=>this.textContent='📋 Salin', 1500);">📋 Salin</button>
          </td>
        </tr>
      `;
    });
    html += `</tbody></table>`;
  } else {
    html += `<p style="color:var(--text-muted); font-size:0.85rem;">Tidak ditemukan akun PPPoE di XML konfigurasi.</p>`;
  }

  // WiFi Credentials
  if (wifis.length > 0) {
    html += `<h5 style="margin:1rem 0 0.5rem 0; color:var(--primary);">📶 Kredensial WiFi (${wifis.length})</h5>`;
    html += `
      <table class="kv-table" style="margin-bottom:1rem;">
        <thead><tr><th>SSID</th><th>Password</th><th>Aksi</th></tr></thead>
        <tbody>
    `;
    wifis.forEach((w) => {
      html += `
        <tr>
          <td><strong>${esc(w.ssid)}</strong></td>
          <td><code>${esc(w.password || '(Open)')}</code></td>
          <td>
            <button class="btn btn-sm btn-outline" onclick="navigator.clipboard.writeText('${esc(w.password || '')}'); this.textContent='✓ Disalin'; setTimeout(()=>this.textContent='📋 Salin', 1500);">📋 Salin</button>
          </td>
        </tr>
      `;
    });
    html += `</tbody></table>`;
  }

  // Admin Logins
  if (admins.length > 0) {
    html += `<h5 style="margin:1rem 0 0.5rem 0; color:var(--primary);">👤 Akun Login Web Router (${admins.length})</h5>`;
    html += `
      <table class="kv-table">
        <thead><tr><th>Username</th><th>Password</th><th>Aksi</th></tr></thead>
        <tbody>
    `;
    admins.forEach((a) => {
      html += `
        <tr>
          <td><code>${esc(a.username)}</code></td>
          <td><code>${esc(a.password)}</code></td>
          <td>
            <button class="btn btn-sm btn-outline" onclick="navigator.clipboard.writeText('${esc(a.password)}'); this.textContent='✓ Disalin'; setTimeout(()=>this.textContent='📋 Salin', 1500);">📋 Salin</button>
          </td>
        </tr>
      `;
    });
    html += `</tbody></table>`;
  }

  html += `</div>`;
  container.innerHTML = html;
}

// Handler: Check Telnet Port
async function handleCheckTelnet() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const port = parseInt($('#telnet-port-input').value, 10) || 23;
  const display = $('#telnet-status-display');
  display.style.display = 'block';
  display.innerHTML = `<span style="color:var(--text-muted);">Memeriksa port ${port}...</span>`;

  try {
    const res = await api(`/routers/${state.selectedId}/telnet/status?port=${port}`);
    if (res.open) {
      display.innerHTML = `<span style="color:#2ea043; font-weight:600;">✅ ${esc(res.message)}</span>`;
    } else {
      display.innerHTML = `<span style="color:#f85149; font-weight:600;">❌ ${esc(res.message)}</span>`;
    }
  } catch (err) {
    display.innerHTML = `<span style="color:#f85149;">Gagal memeriksa port: ${esc(err.message)}</span>`;
  }
}

// Handler: Enable Factory Mode Telnet
async function handleEnableFactoryTelnet() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const display = $('#telnet-status-display');
  display.style.display = 'block';
  display.innerHTML = `<span style="color:var(--text-muted);">Mengirim sinyal Factory Mode (/webFac)...</span>`;

  try {
    const res = await api(`/routers/${state.selectedId}/telnet/enable-factory`, {
      method: 'POST',
      body: JSON.stringify({ mode: 2 }),
    });

    if (res.success) {
      let msg = `<span style="color:#2ea043; font-weight:600;">✅ ${esc(res.message)}</span>`;
      if (res.telnet_user && res.telnet_pass) {
        msg += `<div style="margin-top:6px; font-family:monospace; color:var(--text-main);">User: <strong>${esc(res.telnet_user)}</strong> | Pass: <strong>${esc(res.telnet_pass)}</strong></div>`;
        $('#telnet-user-input').value = res.telnet_user;
        $('#telnet-pass-input').value = res.telnet_pass;
      }
      display.innerHTML = msg;
      showAlert('Sinyal Factory Mode berhasil diproses!', false);
    } else {
      display.innerHTML = `<span style="color:#f85149;">❌ ${esc(res.message)}</span>`;
    }
  } catch (err) {
    display.innerHTML = `<span style="color:#f85149;">Gagal mengaktifkan Factory Mode: ${esc(err.message)}</span>`;
    showAlert(err.message, true);
  }
}

// Handler: Extract Credentials via Telnet (sendcmd)
async function handleExtractTelnet() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const user = $('#telnet-user-input').value.trim() || null;
  const pass = $('#telnet-pass-input').value || null;
  const port = parseInt($('#telnet-port-input').value, 10) || 23;

  const resultBox = $('#telnet-result-box');
  const container = $('#telnet-credentials-container');
  resultBox.style.display = 'block';
  container.innerHTML = `<div style="padding:1rem; text-align:center; color:var(--text-muted);">⏳ Menghubungkan via Telnet & mengeksekusi sendcmd...</div>`;

  try {
    const res = await api(`/routers/${state.selectedId}/telnet/extract`, {
      method: 'POST',
      body: JSON.stringify({
        username: user,
        password: pass,
        port: port,
        timeout: 10.0,
      }),
    });

    renderCredentials(container, res.credentials, 'TELNET (sendcmd)');
    showAlert('Ekstraksi kredensial via Telnet berhasil!', false);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger" style="margin:0;">Gagal ekstrak via Telnet: ${esc(err.message)}</div>`;
    showAlert(err.message, true);
  }
}

// Handler: Run Custom Telnet / Shell Command
async function handleRunTelnetCmd() {
  if (!state.selectedId) {
    showAlert('Pilih router terlebih dahulu', true);
    return;
  }
  const cmd = $('#telnet-cmd-input').value.trim();
  if (!cmd) {
    showAlert('Masukkan perintah terlebih dahulu', true);
    return;
  }
  const user = $('#telnet-user-input').value.trim() || null;
  const pass = $('#telnet-pass-input').value || null;
  const port = parseInt($('#telnet-port-input').value, 10) || 23;

  const term = $('#telnet-terminal-output');
  term.style.display = 'block';
  term.textContent = `> ${cmd}\nMenjalankan perintah via Telnet...`;

  try {
    const res = await api(`/routers/${state.selectedId}/telnet/command`, {
      method: 'POST',
      body: JSON.stringify({
        command: cmd,
        username: user,
        password: pass,
        port: port,
        timeout: 10.0,
      }),
    });
    term.textContent = res.output || '(Tidak ada output)';
  } catch (err) {
    term.textContent = `Error: ${err.message}`;
  }
}

// Export All Discovered DHCP Clients to CSV
function exportAllClientsCsv() {
  const clients = state.collectedClients || [];
  if (clients.length === 0) {
    showAlert('Belum ada data perangkat client yang terkumpul dari re-auth massal.', true);
    return;
  }

  const csvRows = [
    ['"No"', '"IP Router"', '"Nama Router"', '"Model"', '"SSID"', '"IP Client"', '"MAC Address"', '"Hostname / Perangkat"', '"Merek / Kategori"', '"Interface"', '"Waktu Sewa (Lease)"'].join(',')
  ];

  clients.forEach((c, idx) => {
    csvRows.push([
      idx + 1,
      `"${(c.router_ip || '').replace(/"/g, '""')}"`,
      `"${(c.router_name || '').replace(/"/g, '""')}"`,
      `"${(c.router_model || '').replace(/"/g, '""')}"`,
      `"${(c.ssid || '').replace(/"/g, '""')}"`,
      `"${(c.ip || c.ip_address || '').replace(/"/g, '""')}"`,
      `"${(c.mac || c.mac_address || '').replace(/"/g, '""')}"`,
      `"${(c.hostname || '').replace(/"/g, '""')}"`,
      `"${(c.brand || classifyDeviceBrand(c.hostname, c.mac)).replace(/"/g, '""')}"`,
      `"${(c.interface || '').replace(/"/g, '""')}"`,
      `"${(c.last_seen || '').replace(/"/g, '""')}"`,
    ].join(','));
  });

  const blob = new Blob([csvRows.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const now = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  a.href = url;
  a.download = `zte_all_dhcp_clients_${now}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showAlert(`Berhasil mengekspor ${clients.length} perangkat client ke CSV!`, false);
}

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  loadRouters();
  setRefreshInterval();
});