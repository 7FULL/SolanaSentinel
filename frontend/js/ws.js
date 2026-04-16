/**
 * ws.js — Real-time WebSocket client (Socket.IO)
 *
 * Connects to the Flask-SocketIO backend and dispatches incoming events
 * to the relevant UI sections without requiring a manual refresh.
 *
 * Events received from the backend:
 *   token:detected  — new token found by the sniper
 *   ct:trade        — whale transaction detected by copy-trading
 *   log:entry       — new structured log entry
 */

const WS_URL = 'http://127.0.0.1:5005';

// ---------------------------------------------------------------
// Connection management
// ---------------------------------------------------------------

let _socket = null;
let _reconnectTimer = null;

function wsConnect() {
    if (_socket) return;

    _socket = io(WS_URL, {
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionDelay: 2000,
        reconnectionAttempts: Infinity,
    });

    _socket.on('connect', () => {
        console.log('[WS] Connected to backend');
        _updateWsIndicator(true);
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    });

    _socket.on('disconnect', (reason) => {
        console.warn('[WS] Disconnected:', reason);
        _updateWsIndicator(false);
    });

    _socket.on('connect_error', (err) => {
        console.warn('[WS] Connection error:', err.message);
        _updateWsIndicator(false);
    });

    // ── Event handlers ──────────────────────────────────────────

    _socket.on('token:detected', (data) => {
        _onTokenDetected(data);
    });

    _socket.on('ct:trade', (data) => {
        _onCtTrade(data);
    });

    _socket.on('log:entry', (data) => {
        _onLogEntry(data);
    });
}

function wsDisconnect() {
    if (_socket) {
        _socket.disconnect();
        _socket = null;
    }
}

// ---------------------------------------------------------------
// Status indicator in the header
// ---------------------------------------------------------------

function _updateWsIndicator(connected) {
    const dot = document.getElementById('ws-status-dot');
    const txt = document.getElementById('ws-status-text');
    if (dot) dot.className = 'status-dot' + (connected ? '' : ' error');
    if (txt) txt.textContent = connected ? 'Live' : 'Reconnecting…';
}

// ---------------------------------------------------------------
// token:detected  →  update Sniper section
// ---------------------------------------------------------------

function _onTokenDetected(token) {
    // 1. Prepend the new row into the detected-tokens table (matches renderDetectedTokens columns)
    const tbody = document.querySelector('#detected-tokens-table tbody');
    if (tbody) {
        // Remove "no tokens" placeholder row if present
        const placeholder = tbody.querySelector('td[colspan]');
        if (placeholder) placeholder.closest('tr').remove();

        const row = _buildDetectedTokenRow(token);
        if (row) {
            tbody.insertAdjacentHTML('afterbegin', row);
            // Trim to 200 rows so the DOM doesn't grow unbounded
            const rows = tbody.querySelectorAll('tr');
            if (rows.length > 200) rows[rows.length - 1].remove();
            _flashRow(tbody.querySelector('tr'));
        }
    }

    // 2. Increment the "detected today" counter in the sniper stats bar
    const counter = document.getElementById('sniper-detected-today');
    if (counter) {
        const n = parseInt(counter.textContent) || 0;
        counter.textContent = n + 1;
    }

    // 3. Toast when sniper section is not visible
    const sniperSection = document.getElementById('sniper-section');
    if (!sniperSection || !sniperSection.classList.contains('active')) {
        _showToast(
            `New token: ${token.token_symbol || '?'} (${token.source || 'unknown'})`,
            'info', 3000
        );
    }
}

/**
 * Build a table row matching the #detected-tokens-table column layout:
 * Token | Platform | Liquidity | Market Cap | Risk Score | Status | Action | Time
 */
function _buildDetectedTokenRow(token) {
    if (!token) return null;

    const symbol   = token.token_symbol || token.token_name || '?';
    const name     = token.token_name   || symbol;
    const platform = token.source       || 'unknown';
    const liq      = token.initial_liquidity ?? 0;
    const mc       = token.market_cap        ?? 0;
    const risk     = token.risk_score        ?? 100;
    const status   = token.status            || 'detected';
    const action   = token.action_taken      || 'pending';
    const mint     = token.token_mint        || '';
    const time     = token.detected_at ? _reltime(token.detected_at) : 'just now';

    const riskClass = risk >= 70 ? 'text-success' : risk >= 50 ? 'text-warning' : 'text-danger';

    const solscanUrl = token.solscan_url     || null;
    const dexUrl     = token.dexscreener_url || null;
    const pumpUrl    = (platform === 'pumpfun' && mint) ? `https://pump.fun/${mint}` : null;
    const links = [
        solscanUrl ? `<span style="cursor:pointer;color:#58a6ff" onclick="window.open('${_esc(solscanUrl)}','_blank')">Tx</span>` : '',
        dexUrl     ? `<span style="cursor:pointer;color:#14f195" onclick="window.open('${_esc(dexUrl)}','_blank')">Chart</span>` : '',
        pumpUrl    ? `<span style="cursor:pointer;color:#ff6b35" onclick="window.open('${_esc(pumpUrl)}','_blank')">Pump</span>` : '',
    ].filter(Boolean).join(' · ');

    return `<tr>
        <td>
            <strong>${_esc(symbol)}</strong><br>
            <small class="text-muted">${_esc(name)}${links ? '<br><small>' + links + '</small>' : ''}</small>
        </td>
        <td><span class="badge">${_esc(platform)}</span></td>
        <td>${Number(liq).toLocaleString()}</td>
        <td>${Number(mc).toLocaleString()}</td>
        <td><span class="${riskClass}">${risk}/100</span></td>
        <td><span class="badge ${_esc(status)}">${_esc(status).replace(/_/g,' ')}</span></td>
        <td><span class="badge">${_esc(action).replace(/_/g,' ')}</span></td>
        <td>${time}</td>
    </tr>`;
}

// ---------------------------------------------------------------
// ct:trade  →  update Copy-Trading activity tab
// ---------------------------------------------------------------

function _onCtTrade(trade) {
    // Prepend to activity list if it's in the DOM
    const container = document.getElementById('ct-activity-list');
    if (container) {
        // Remove the "no activity" placeholder
        const placeholder = container.querySelector('p.text-muted');
        if (placeholder) placeholder.remove();

        const icon = trade.type === 'buy' ? '🟢' : '🔴';
        const amt  = trade.sol_change != null
            ? `<span class="text-success">${parseFloat(trade.sol_change).toFixed(4)} SOL</span>`
            : '—';

        const html = `
        <div class="ct-activity-row" style="animation:fadeIn .3s">
            <span class="ct-activity-icon">${icon}</span>
            <div class="ct-activity-main">
                <span class="ct-activity-label">${_esc(trade.wallet_name || trade.wallet_address?.slice(0,8) || '?')}</span>
                <span class="ct-activity-action">${(trade.type || '?').toUpperCase()} ${_esc(trade.token_symbol || '?')}</span>
            </div>
            <div class="ct-activity-amounts">
                <span title="SOL amount">${amt}</span>
            </div>
            <div class="ct-activity-meta">
                <span class="text-muted">just now</span>
            </div>
        </div>`;

        container.insertAdjacentHTML('afterbegin', html);

        // Trim
        const items = container.querySelectorAll('.ct-activity-row');
        if (items.length > 100) items[items.length - 1].remove();
        _flashRow(container.querySelector('.ct-activity-row'));
    }

    // Toast if section not visible
    const ctSection = document.getElementById('copy-trading-section');
    if (!ctSection || !ctSection.classList.contains('active')) {
        _showToast(
            `Copy trade: ${trade.wallet_name || '?'} ${trade.type || '?'} ${trade.token_symbol || '?'}`,
            'info', 4000
        );
    }
}

// ---------------------------------------------------------------
// log:entry  →  update Logs section
// ---------------------------------------------------------------

function _onLogEntry(entry) {
    const container = document.getElementById('logs-list');
    if (!container) return;

    // Only stream INFO and above to avoid flooding the UI with DEBUG noise
    const level = (entry.level || '').toUpperCase();
    if (level === 'DEBUG') return;

    const levelClass = {
        INFO:     'text-primary',
        WARNING:  'text-warning',
        ERROR:    'text-danger',
        CRITICAL: 'text-danger',
    }[level] || '';

    const time = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : '';

    const html = `
    <div class="log-entry log-${level.toLowerCase()}" style="animation:fadeIn .2s">
        <span class="log-time text-muted">${time}</span>
        <span class="log-level ${levelClass}">[${level}]</span>
        <span class="log-module text-muted">[${_esc(entry.module || 'system')}]</span>
        <span class="log-message">${_esc(entry.message || '')}</span>
    </div>`;

    container.insertAdjacentHTML('afterbegin', html);

    // Trim DOM
    const entries = container.querySelectorAll('.log-entry');
    if (entries.length > 500) entries[entries.length - 1].remove();
}

// ---------------------------------------------------------------
// Toast notifications (non-intrusive, bottom-right)
// ---------------------------------------------------------------

function _showToast(message, type = 'info', duration = 3000) {
    let container = document.getElementById('ws-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'ws-toast-container';
        container.style.cssText = `
            position:fixed; bottom:20px; right:20px; z-index:9999;
            display:flex; flex-direction:column; gap:8px; pointer-events:none;
        `;
        document.body.appendChild(container);
    }

    const colors = { info: '#3b82f6', success: '#10b981', warning: '#f59e0b', error: '#ef4444' };
    const toast = document.createElement('div');
    toast.style.cssText = `
        background:var(--bg-secondary);
        border:1px solid ${colors[type] || colors.info};
        border-left:4px solid ${colors[type] || colors.info};
        border-radius:6px; padding:10px 14px;
        color:var(--text-primary); font-size:.85rem;
        max-width:320px; opacity:0;
        transition:opacity .25s; pointer-events:auto;
        box-shadow:0 4px 12px rgba(0,0,0,.3);
    `;
    toast.textContent = message;
    container.appendChild(toast);

    // Fade in
    requestAnimationFrame(() => { toast.style.opacity = '1'; });

    // Fade out and remove
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

function _flashRow(el) {
    if (!el) return;
    el.style.transition = 'background .1s';
    el.style.background = 'rgba(59,130,246,.18)';
    setTimeout(() => { el.style.background = ''; }, 800);
}

function _fmt(n) {
    if (n == null) return '?';
    n = parseFloat(n);
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
    return n.toFixed(0);
}

function _reltime(iso) {
    if (!iso) return '';
    const diff = (Date.now() - new Date(iso)) / 1000;
    if (diff < 60)  return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    return Math.floor(diff / 3600) + 'h ago';
}

function _esc(s) {
    return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---------------------------------------------------------------
// Auto-connect on page load
// ---------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => wsConnect());
