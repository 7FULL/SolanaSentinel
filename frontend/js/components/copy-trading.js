/**
 * Copy Trading Component
 *
 * Manages the three-tab copy trading UI:
 *   1. Monitored Wallets — add/edit/remove wallets with full per-wallet config
 *   2. Activity          — live feed of detected trades
 *   3. Simulation P&L   — virtual position tracking with refresh / reset
 */

// Active simulation filter ('all' | 'open' | 'closed')
let _ctSimFilter = 'open';

// Auto-refresh timer — started when the section loads, self-cancels on navigate away
let _ctRefreshTimer = null;

function _startCTAutoRefresh() {
    _stopCTAutoRefresh();
    _ctRefreshTimer = setInterval(async () => {
        // Self-cancel if the user navigated away from copy-trading
        if (typeof appState !== 'undefined' && appState.currentSection !== 'copy-trading') {
            _stopCTAutoRefresh();
            return;
        }
        await loadCTSimPositions();
        const activityTab = document.getElementById('ct-tab-activity');
        if (activityTab && activityTab.style.display !== 'none') {
            await loadCTActivity();
        }
    }, 10000);
}

function _stopCTAutoRefresh() {
    if (_ctRefreshTimer) { clearInterval(_ctRefreshTimer); _ctRefreshTimer = null; }
}

// ---------------------------------------------------------------
// Section entry point
// ---------------------------------------------------------------

async function loadCopyTradingData() {
    try {
        const status = await api.get('/copy-trading/status').catch(() => null);
        if (status?.success) _updateCTStatusBar(status.data);
    } catch (_) {}

    await Promise.all([
        loadCTWallets(),
        loadCTSimPositions(),
    ]);

    // Start auto-refresh for this section (10s interval, self-cancels on navigate away)
    _startCTAutoRefresh();
}

// ---------------------------------------------------------------
// Status bar helpers
// ---------------------------------------------------------------

function _updateCTStatusBar(data) {
    const isRunning = data?.running ?? false;

    const startBtn = document.getElementById('ct-start-btn');
    const stopBtn  = document.getElementById('ct-stop-btn');
    const statusEl = document.getElementById('ct-status-text');

    if (startBtn) startBtn.style.display = isRunning ? 'none'  : '';
    if (stopBtn)  stopBtn.style.display  = isRunning ? ''      : 'none';
    if (statusEl) {
        statusEl.textContent  = isRunning ? 'Running' : 'Stopped';
        statusEl.className    = 'stat-number ' + (isRunning ? 'text-success' : 'text-muted');
    }

    const countEl = document.getElementById('ct-monitored-count');
    if (countEl) countEl.textContent = data?.monitored_wallets ?? 0;

    const copiedEl = document.getElementById('ct-copied-today');
    if (copiedEl) copiedEl.textContent = data?.trades_today ?? 0;
}

// ---------------------------------------------------------------
// Start / Stop
// ---------------------------------------------------------------

async function startCopyTradingHandler() {
    try {
        const resp = await api.startCopyTrading();
        if (resp.success) {
            showSuccess('Copy trading started');
            _updateCTStatusBar({ running: true });
        }
    } catch (e) {
        showError('Failed to start copy trading');
    }
}

async function stopCopyTradingHandler() {
    const confirmed = await showConfirm(
        'Stop copy trading? Active monitoring will pause.',
        'Stop', 'Cancel'
    );
    if (!confirmed) return;

    try {
        const resp = await api.stopCopyTrading();
        if (resp.success) {
            showSuccess('Copy trading stopped');
            _updateCTStatusBar({ running: false });
        }
    } catch (e) {
        showError('Failed to stop copy trading');
    }
}

// ---------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------

function switchCTTab(tabName, btn) {
    if (typeof appState !== 'undefined') appState.activeCTTab = tabName;
    document.querySelectorAll('.ct-tab-content').forEach(el => el.style.display = 'none');
    document.querySelectorAll('#copy-trading-section .tab-btn').forEach(b => b.classList.remove('active'));

    const tab = document.getElementById(`ct-tab-${tabName}`);
    if (tab) tab.style.display = '';
    if (btn) btn.classList.add('active');

    if (tabName === 'activity')   loadCTActivity();
    if (tabName === 'simulation') loadCTSimPositions();
}

// ---------------------------------------------------------------
// Tab 1 — Monitored Wallets
// ---------------------------------------------------------------

async function loadCTWallets() {
    try {
        const resp = await api.getCTWallets();
        if (resp.success) renderCTWallets(resp.data);
    } catch (e) {
        console.error('Failed to load CT wallets:', e);
    }
}

const _FOLLOW_LABELS = {
    simple: { icon: '👁',  text: 'Simple' },
    deep:   { icon: '🔍', text: 'Deep'   },
};

const _MODE_LABELS = {
    notify:   { icon: '🔔', text: 'Notify',   cls: 'badge-info'    },
    simulate: { icon: '🎮', text: 'Simulate', cls: 'badge-warning' },
    auto:     { icon: '⚡', text: 'Auto',     cls: 'badge-success' },
    precise:  { icon: '🎯', text: 'Precise',  cls: 'badge-primary' },
};

function renderCTWallets(wallets) {
    const container = document.getElementById('ct-wallets-list');
    if (!container) return;

    if (!wallets || wallets.length === 0) {
        container.innerHTML = '<p class="text-muted">No wallets monitored yet. Click <strong>Add Wallet</strong> to get started.</p>';
        return;
    }

    // Separate user-added wallets from auto sub-wallets
    const primary = wallets.filter(w => !w.is_sub_wallet);
    const subs    = wallets.filter(w =>  w.is_sub_wallet);

    const renderWallet = (w) => {
        const follow = _FOLLOW_LABELS[w.follow_mode] || { icon: '?', text: w.follow_mode };
        const mode   = _MODE_LABELS[w.execution_mode]  || { icon: '?', text: w.execution_mode, cls: '' };
        const enabled = w.enabled !== false;
        const subTag  = w.is_sub_wallet
            ? `<span class="badge" style="font-size:.65rem;opacity:.7">sub-wallet</span>` : '';

        return `
        <div class="ct-wallet-row ${enabled ? '' : 'ct-wallet-disabled'}">
            <div class="ct-wallet-main">
                <div class="ct-wallet-name">
                    ${w.label} ${subTag}
                    ${w.parent_wallet
                        ? `<span class="text-muted" style="font-size:.7rem">↑ ${shortenAddress(w.parent_wallet)}</span>`
                        : ''}
                </div>
                <div class="ct-wallet-addr address-short"
                     onclick="copyToClipboard('${w.address}')"
                     title="Click to copy">${shortenAddress(w.address)}</div>
            </div>
            <div class="ct-wallet-badges">
                <span class="badge">${follow.icon} ${follow.text}</span>
                <span class="badge ${mode.cls}">${mode.icon} ${mode.text}</span>
            </div>
            <div class="ct-wallet-stats">
                <span title="Trades detected"><strong>${w.stats?.trades_detected ?? 0}</strong> detected</span>
                <span title="Trades copied"><strong>${w.stats?.trades_copied ?? 0}</strong> copied</span>
                <span title="Last trade">${w.stats?.last_trade_at ? formatRelativeTime(w.stats.last_trade_at) : 'Never'}</span>
            </div>
            <div class="ct-wallet-actions">
                <button class="btn btn-sm ${enabled ? 'btn-secondary' : 'btn-success'}"
                        onclick="toggleCTWallet('${w.address}')">
                    ${enabled ? 'Pause' : 'Resume'}
                </button>
                <button class="btn btn-sm btn-primary"
                        onclick="showEditCTWalletModal('${w.address}')">Edit</button>
                <button class="btn btn-sm btn-danger"
                        onclick="removeCTWallet('${w.address}')">Remove</button>
            </div>
        </div>`;
    };

    let html = primary.map(renderWallet).join('');

    if (subs.length > 0) {
        html += `<div class="ct-sub-wallets-section">
            <h4 class="ct-sub-label">Auto-detected sub-wallets (deep follow)</h4>
            ${subs.map(renderWallet).join('')}
        </div>`;
    }

    container.innerHTML = html;
    _addCTStyles();
}

// ---------------------------------------------------------------
// Add / Edit wallet modal
// ---------------------------------------------------------------

function showAddCTWalletModal() {
    _showCTWalletModal(null);
}

async function showEditCTWalletModal(address) {
    try {
        const resp = await api.getCTWallets();
        if (resp.success) {
            const wallet = resp.data.find(w => w.address === address);
            if (wallet) _showCTWalletModal(wallet);
        }
    } catch (e) {
        showError('Failed to load wallet');
    }
}

function _showCTWalletModal(wallet) {
    const isEdit = !!wallet;
    const w = wallet || {};
    const f = w.filters   || {};
    const e = w.execution || {};
    const l = w.limits    || {};

    const content = `
        <form novalidate onsubmit="saveCTWalletHandler(event, ${isEdit ? `'${w.address}'` : 'null'})">
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label required">Wallet Label</label>
                    <input type="text" id="ctw-label" value="${w.label || ''}"
                           placeholder="e.g. Whale #1" required>
                </div>
                <div class="form-group">
                    <label class="form-label required">Wallet Address</label>
                    <input type="text" id="ctw-address" value="${w.address || ''}"
                           placeholder="Solana public key" required
                           ${isEdit ? 'readonly style="opacity:.6"' : ''}>
                </div>
            </div>

            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Follow Mode</label>
                    <select id="ctw-follow-mode">
                        <option value="simple" ${(w.follow_mode||'simple')==='simple'?'selected':''}>
                            Simple — monitor this wallet only
                        </option>
                        <option value="deep" ${w.follow_mode==='deep'?'selected':''}>
                            Deep — also follow funded sub-wallets
                        </option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Execution Mode</label>
                    <select id="ctw-exec-mode" onchange="updateCTModeFields()">
                        <option value="notify"   ${(w.execution_mode||'notify')==='notify'  ?'selected':''}>Notify only</option>
                        <option value="simulate" ${w.execution_mode==='simulate'?'selected':''}>Simulate (track P&amp;L)</option>
                        <option value="auto"     ${w.execution_mode==='auto'    ?'selected':''}>Auto (fixed SOL amount)</option>
                        <option value="precise"  ${w.execution_mode==='precise' ?'selected':''}>Precise (% of wallet balance)</option>
                    </select>
                </div>
            </div>

            <!-- Mode-dependent execution fields -->
            <div id="ctw-field-fixed" class="form-group" style="display:none">
                <label class="form-label">Amount per copy (SOL)</label>
                <input type="number" id="ctw-fixed-amount" step="any" min="0"
                       value="${e.fixed_amount_sol ?? 0.1}">
                <div class="form-helper">Fixed SOL invested every time the whale buys</div>
            </div>
            <div id="ctw-field-pct" class="form-group" style="display:none">
                <label class="form-label">Copy percentage (%)</label>
                <input type="number" id="ctw-copy-pct" step="0.1" min="0.1" max="100"
                       value="${e.copy_percentage ?? 5}">
                <div class="form-helper">
                    % of your assigned wallet balance to invest per copy.<br>
                    Example: 5% on a 2 SOL wallet = 0.10 SOL per trade.
                </div>
            </div>

            <h4>Filters</h4>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Min trade size (SOL)</label>
                    <input type="number" id="ctw-min-trade" step="0.01" min="0"
                           value="${f.min_trade_sol ?? 0.05}">
                    <div class="form-helper">Ignore trades smaller than this</div>
                </div>
                <div class="form-group">
                    <label class="form-label">Min Liquidity (USD)</label>
                    <input type="number" id="ctw-min-liq" min="0"
                           value="${f.min_liquidity ?? 0}">
                </div>
                <div class="form-group">
                    <label class="form-label">Max Market Cap (USD)</label>
                    <input type="number" id="ctw-max-mc" min="0"
                           placeholder="No limit"
                           value="${f.max_market_cap ?? ''}">
                </div>
                <div class="form-group">
                    <label class="form-label">Max slippage (%)</label>
                    <input type="number" id="ctw-slippage" step="any" min="0"
                           value="${e.max_slippage ?? 5}">
                </div>
            </div>

            <div class="form-group">
                <label class="form-label">Operations to copy</label>
                <div style="display:flex;gap:16px;margin-top:4px">
                    <label class="checkbox">
                        <input type="checkbox" id="ctw-op-buy"
                               ${(f.operation_types||['buy','sell']).includes('buy') ?'checked':''}>
                        <span>Buy</span>
                    </label>
                    <label class="checkbox">
                        <input type="checkbox" id="ctw-op-sell"
                               ${(f.operation_types||['buy','sell']).includes('sell')?'checked':''}>
                        <span>Sell</span>
                    </label>
                </div>
            </div>

            <h4>Limits</h4>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Max buys per hour</label>
                    <input type="number" id="ctw-max-buys" step="1" min="1"
                           value="${l.max_buys_per_hour ?? 5}">
                </div>
                <div class="form-group">
                    <label class="form-label">Max position (SOL)</label>
                    <input type="number" id="ctw-max-pos" step="any" min="0"
                           value="${l.max_position_sol ?? 1}">
                    <div class="form-helper">Hard cap per single copy trade</div>
                </div>
            </div>

            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button type="submit" class="btn btn-primary">${isEdit ? 'Save Changes' : 'Add Wallet'}</button>
            </div>
        </form>
    `;

    showModal(isEdit ? `Edit: ${w.label}` : 'Add Monitored Wallet', content);
    updateCTModeFields();
}

function updateCTModeFields() {
    const mode = document.getElementById('ctw-exec-mode')?.value;
    const fixedEl = document.getElementById('ctw-field-fixed');
    const pctEl   = document.getElementById('ctw-field-pct');
    if (!fixedEl || !pctEl) return;

    fixedEl.style.display = (mode === 'auto' || mode === 'simulate') ? '' : 'none';
    pctEl.style.display   = (mode === 'precise') ? '' : 'none';
}

async function saveCTWalletHandler(event, existingAddress) {
    event.preventDefault();

    const mode = document.getElementById('ctw-exec-mode').value;
    const ops  = [];
    if (document.getElementById('ctw-op-buy').checked)  ops.push('buy');
    if (document.getElementById('ctw-op-sell').checked) ops.push('sell');

    const maxMcVal = document.getElementById('ctw-max-mc').value;

    const data = {
        address:        document.getElementById('ctw-address').value.trim(),
        label:          document.getElementById('ctw-label').value.trim(),
        follow_mode:    document.getElementById('ctw-follow-mode').value,
        execution_mode: mode,
        filters: {
            operation_types:   ops,
            min_trade_sol:     parseFloat(document.getElementById('ctw-min-trade').value) || 0,
            min_liquidity:     parseInt(document.getElementById('ctw-min-liq').value) || 0,
            max_market_cap:    maxMcVal ? parseInt(maxMcVal) : null,
            allowed_platforms: [],
        },
        execution: {
            fixed_amount_sol: parseFloat(document.getElementById('ctw-fixed-amount').value) || 0.1,
            copy_percentage:  parseFloat(document.getElementById('ctw-copy-pct').value)     || 5.0,
            max_slippage:     parseFloat(document.getElementById('ctw-slippage').value)     || 5.0,
        },
        limits: {
            max_buys_per_hour: parseInt(document.getElementById('ctw-max-buys').value) || 5,
            max_position_sol:  parseFloat(document.getElementById('ctw-max-pos').value) || 1.0,
        },
    };

    try {
        let resp;
        if (existingAddress) {
            resp = await api.updateCTWallet(existingAddress, data);
        } else {
            resp = await api.addCTWallet(data);
        }

        if (resp.success) {
            closeModal();
            showSuccess(existingAddress ? 'Wallet updated' : 'Wallet added');
            await loadCTWallets();
        }
    } catch (e) {
        showError('Failed to save wallet: ' + e.message);
    }
}

async function toggleCTWallet(address) {
    try {
        const resp = await api.toggleCTWallet(address);
        if (resp.success) await loadCTWallets();
    } catch (e) {
        showError('Failed to toggle wallet');
    }
}

async function removeCTWallet(address) {
    const confirmed = await showConfirm(
        'Remove this wallet from monitoring? Existing simulation positions are not affected.',
        'Remove', 'Cancel'
    );
    if (!confirmed) return;

    try {
        const resp = await api.removeCTWallet(address);
        if (resp.success) {
            showSuccess('Wallet removed');
            await loadCTWallets();
        }
    } catch (e) {
        showError('Failed to remove wallet');
    }
}

// ---------------------------------------------------------------
// Tab 2 — Activity feed
// ---------------------------------------------------------------

async function loadCTActivity() {
    try {
        const resp = await api.getCopyTradingHistory(100);
        if (resp.success) renderCTActivity(resp.data);
    } catch (e) {
        console.error('Failed to load CT activity:', e);
    }
}

function renderCTActivity(entries) {
    const container = document.getElementById('ct-activity-list');
    if (!container) return;

    if (!entries || entries.length === 0) {
        container.innerHTML = '<p class="text-muted">No activity yet.</p>';
        return;
    }

    const actionIcon = { buy: '🟢', sell: '🔴' };

    const rows = entries.map(e => {
        const icon    = actionIcon[e.action] || '⚪';
        const amtOurs = e.our_amount_sol != null
            ? `<span class="text-success">${e.our_amount_sol.toFixed(4)} SOL</span>`
            : '—';
        return `
        <div class="ct-activity-row">
            <span class="ct-activity-icon">${icon}</span>
            <div class="ct-activity-main">
                <span class="ct-activity-label">${e.source_label}</span>
                <span class="ct-activity-action">${e.action.toUpperCase()} ${e.token_symbol}</span>
            </div>
            <div class="ct-activity-amounts">
                <span title="Whale amount">${e.whale_amount_sol?.toFixed(4) ?? '?'} SOL</span>
                <span title="Our copy amount">${amtOurs}</span>
            </div>
            <div class="ct-activity-meta">
                <span class="badge">${e.execution_mode}</span>
                <span class="text-muted">${formatRelativeTime(e.timestamp)}</span>
            </div>
        </div>`;
    }).join('');

    container.innerHTML = rows;
}

// ---------------------------------------------------------------
// Tab 3 — Simulation P&L
// ---------------------------------------------------------------

async function loadCTSimPositions() {
    try {
        // Pass the active filter so we only load what's shown by default (open positions).
        // Passing null fetches all statuses (used when _ctSimFilter === 'all').
        const statusParam = _ctSimFilter === 'all' ? null : _ctSimFilter;
        const resp = await api.getCTSimPositions(statusParam);
        if (resp.success) {
            renderCTSimStats(resp.data.stats);
            _renderCTSimPositions(resp.data.positions);
            // Update header P&L stat
            const pnlEl = document.getElementById('ct-sim-pnl');
            if (pnlEl) {
                const pnl = resp.data.stats?.total_pnl_sol ?? 0;
                pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4) + ' SOL';
                pnlEl.className   = 'stat-number ' + (pnl >= 0 ? 'text-success' : 'text-danger');
            }
        }
    } catch (e) {
        console.error('Failed to load CT sim positions:', e);
    }
}

function renderCTSimStats(stats) {
    if (!stats) return;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };

    const pnlClass = (v) => v >= 0 ? 'text-success' : 'text-danger';
    const fmt      = (v, suffix = '') => (v >= 0 ? '+' : '') + v.toFixed(4) + suffix;

    set('ct-stat-total',      stats.total_trades);
    set('ct-stat-open',       stats.open_count);
    set('ct-stat-winrate',    stats.win_rate + '%');
    set('ct-stat-best',       (stats.best_trade_pct ?? 0).toFixed(2) + '%');
    set('ct-stat-worst',      (stats.worst_trade_pct ?? 0).toFixed(2) + '%');

    ['ct-stat-realized', 'ct-stat-unrealized', 'ct-stat-total-pnl'].forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        const v = [stats.realized_pnl_sol, stats.unrealized_pnl_sol, stats.total_pnl_sol][i] ?? 0;
        el.textContent = fmt(v, ' SOL');
        el.className   = 'stat-number ' + pnlClass(v);
    });
}

function _renderCTSimPositions(positions) {
    const tbody = document.getElementById('ct-sim-positions-body');
    if (!tbody) return;

    const filtered = _ctSimFilter === 'all'
        ? positions
        : positions.filter(p => p.status === _ctSimFilter);

    if (!filtered || filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No positions</td></tr>';
        return;
    }

    const statusBadge = (p) => {
        if (p.status === 'open')   return '<span class="badge active">Open</span>';
        if (p.status === 'closed') {
            const reason = p.exit_reason === 'whale_sold' ? '🐳 Whale sold'
                         : p.exit_reason === 'manual'     ? '✋ Manual'
                         : p.exit_reason || 'Closed';
            return `<span class="badge inactive" title="${reason}">Closed</span>`;
        }
        return `<span class="badge">${p.status}</span>`;
    };

    tbody.innerHTML = filtered.map(p => {
        const pnlPct = p.pnl_percent ?? 0;
        const pnlSol = p.pnl_sol     ?? 0;
        const pnlCls = pnlPct >= 0   ? 'text-success' : 'text-danger';

        return `<tr>
            <td title="${p.source_wallet}">${p.source_label}</td>
            <td><strong>${p.token_symbol}</strong></td>
            <td>$${(p.entry_price   || 0).toFixed(8)}</td>
            <td>$${(p.current_price || 0).toFixed(8)}</td>
            <td class="${pnlCls}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</td>
            <td class="${pnlCls}">${pnlSol >= 0 ? '+' : ''}${pnlSol.toFixed(4)}</td>
            <td>${(p.simulated_sol || 0).toFixed(4)} SOL</td>
            <td><span class="badge">${p.execution_mode}</span></td>
            <td>${statusBadge(p)}</td>
            <td>
                ${p.status === 'open'
                    ? `<button class="btn btn-sm btn-danger"
                               onclick="closeCTSimPositionHandler('${p.id}')">Close</button>`
                    : '—'}
            </td>
        </tr>`;
    }).join('');
}

function filterCTSimPositions(value) {
    _ctSimFilter = value;
    loadCTSimPositions();
}

async function refreshCTSimNow(btn) {
    const original = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    try {
        const resp = await api.refreshCTSimPrices();
        if (resp.success) {
            renderCTSimStats(resp.data.stats);
            _renderCTSimPositions(resp.data.positions);
        }
    } catch (e) {
        showError('Failed to refresh prices');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = original; }
    }
}

async function resetCTSimulation() {
    const confirmed = await showConfirm(
        'Clear all copy trading simulation positions? This cannot be undone.',
        'Reset', 'Cancel'
    );
    if (!confirmed) return;

    try {
        const resp = await api.resetCTSimulation();
        if (resp.success) {
            showSuccess('Simulation reset');
            await loadCTSimPositions();
        }
    } catch (e) {
        showError('Failed to reset simulation');
    }
}

async function closeCTSimPositionHandler(positionId) {
    try {
        const resp = await api.closeCTSimPosition(positionId);
        if (resp.success) {
            showSuccess('Position closed');
            await loadCTSimPositions();
        }
    } catch (e) {
        showError('Failed to close position');
    }
}

// ---------------------------------------------------------------
// Styles (injected once)
// ---------------------------------------------------------------

function _addCTStyles() {
    if (document.getElementById('ct-component-styles')) return;
    const style = document.createElement('style');
    style.id = 'ct-component-styles';
    style.textContent = `
        /* Wallet rows */
        .ct-wallet-row {
            display: flex;
            align-items: center;
            gap: var(--spacing-md);
            padding: var(--spacing-sm) var(--spacing-md);
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            margin-bottom: var(--spacing-sm);
            flex-wrap: wrap;
        }
        .ct-wallet-disabled { opacity: .5; }
        .ct-wallet-main {
            flex: 1;
            min-width: 160px;
        }
        .ct-wallet-name {
            font-weight: 600;
            font-size: .875rem;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .ct-wallet-addr {
            font-size: .75rem;
            color: var(--text-muted);
            cursor: pointer;
        }
        .ct-wallet-badges {
            display: flex;
            gap: var(--spacing-sm);
        }
        .ct-wallet-stats {
            display: flex;
            gap: var(--spacing-md);
            font-size: .75rem;
            color: var(--text-secondary);
        }
        .ct-wallet-actions {
            display: flex;
            gap: var(--spacing-sm);
            margin-left: auto;
        }
        .ct-sub-wallets-section {
            margin-top: var(--spacing-md);
            padding-top: var(--spacing-md);
            border-top: 1px solid var(--border-color);
        }
        .ct-sub-label {
            font-size: .75rem;
            color: var(--text-muted);
            margin-bottom: var(--spacing-sm);
        }

        /* Activity feed */
        .ct-activity-row {
            display: flex;
            align-items: center;
            gap: var(--spacing-md);
            padding: var(--spacing-sm) 0;
            border-bottom: 1px solid var(--border-color);
        }
        .ct-activity-icon { font-size: 1.1rem; }
        .ct-activity-main {
            flex: 1;
            display: flex;
            flex-direction: column;
        }
        .ct-activity-label  { font-weight: 600; font-size: .85rem; }
        .ct-activity-action { font-size: .75rem; color: var(--text-muted); }
        .ct-activity-amounts {
            display: flex;
            flex-direction: column;
            font-size: .8rem;
            text-align: right;
        }
        .ct-activity-meta {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            font-size: .75rem;
            gap: 2px;
        }
    `;
    document.head.appendChild(style);
}
