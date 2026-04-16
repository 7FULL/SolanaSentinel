/**
 * Sniper Bot Component
 */

// Currently active tracker filter
let _trackerFilter = 'all';

// Live detection: show only watching tokens by default
let _liveShowAll = false;

// Auto-refresh timer — started when the section loads, self-cancels on navigate away
let _sniperRefreshTimer = null;

function _startSniperAutoRefresh() {
    _stopSniperAutoRefresh();
    _sniperRefreshTimer = setInterval(async () => {
        if (typeof appState !== 'undefined' && appState.currentSection !== 'sniper') {
            _stopSniperAutoRefresh();
            return;
        }
        // Live tab: always refresh (existing dashboard.js interval also does this, harmless overlap)
        const livePanel = document.getElementById('sniper-panel-live');
        if (livePanel && livePanel.style.display !== 'none') {
            await loadDetectedTokens();
        }
        // Tracker tab
        const trackerPanel = document.getElementById('sniper-panel-tracker');
        if (trackerPanel && trackerPanel.style.display !== 'none') {
            await loadTokenTracker();
        }
        // Simulation tab
        const simPanel = document.getElementById('sniper-panel-simulation');
        if (simPanel && simPanel.style.display !== 'none') {
            await loadSimulation();
        }
    }, 5000);
}

function _stopSniperAutoRefresh() {
    if (_sniperRefreshTimer) { clearInterval(_sniperRefreshTimer); _sniperRefreshTimer = null; }
}

async function loadSniperData() {
    await Promise.all([
        loadSniperStatus(),
        loadDetectedTokens()
    ]);
    _startSniperAutoRefresh();
}

async function loadSniperStatus() {
    try {
        const response = await api.getStatus();

        if (response.success) {
            const sniper = response.data.modules.sniper;

            document.getElementById('sniper-status-detail').textContent = sniper.running ? 'Running' : 'Stopped';
            document.getElementById('sniper-mode-detail').textContent = sniper.mode;
            document.getElementById('sniper-detected-today').textContent = sniper.tokens_detected_today;
            document.getElementById('sniper-watching-count').textContent = sniper.tokens_watching ?? 0;

            const startBtn = document.getElementById('sniper-start-btn');
            const stopBtn  = document.getElementById('sniper-stop-btn');
            if (sniper.running) {
                startBtn.style.display = 'none';
                stopBtn.style.display  = 'inline-flex';
            } else {
                startBtn.style.display = 'inline-flex';
                stopBtn.style.display  = 'none';
            }
        }
    } catch (error) {
        console.error('Failed to load sniper status:', error);
    }
}

async function loadDetectedTokens() {
    try {
        const endpoint = _liveShowAll
            ? '/sniper/detected-tokens?limit=500'
            : '/sniper/detected-tokens?limit=200&status=watching';
        const response = await api.get(endpoint);
        if (response.success) {
            renderDetectedTokens(response.data, _liveShowAll);
        }
    } catch (error) {
        console.error('Failed to load detected tokens:', error);
    }
}

function toggleLiveView(btn) {
    _liveShowAll = !_liveShowAll;
    btn.textContent = _liveShowAll ? 'Watching Only' : 'View All';
    btn.title = _liveShowAll ? 'Show only watching tokens' : 'Show all detected tokens';
    // Update header label
    const label = document.getElementById('live-panel-subtitle');
    if (label) label.textContent = _liveShowAll ? '— all history' : '— watching';
    loadDetectedTokens();
}

// ── Tab switching ────────────────────────────────────────────────────────────

function switchSniperTab(tab) {
    document.getElementById('sniper-panel-live').style.display       = tab === 'live'       ? '' : 'none';
    document.getElementById('sniper-panel-tracker').style.display    = tab === 'tracker'    ? '' : 'none';
    document.getElementById('sniper-panel-simulation').style.display = tab === 'simulation' ? '' : 'none';
    document.getElementById('tab-live').classList.toggle('active',       tab === 'live');
    document.getElementById('tab-tracker').classList.toggle('active',    tab === 'tracker');
    document.getElementById('tab-simulation').classList.toggle('active', tab === 'simulation');
    if (tab === 'tracker')    loadTokenTracker();
    if (tab === 'simulation') loadSimulation();
}

// ── Token Tracker ────────────────────────────────────────────────────────────

function setTrackerFilter(filter, btn) {
    _trackerFilter = filter;
    document.querySelectorAll('.tracker-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    loadTokenTracker();
}

async function loadTokenTracker() {
    const endpoint = _trackerFilter === 'all'
        ? '/sniper/detected-tokens?limit=200'
        : `/sniper/detected-tokens?limit=200&status=${_trackerFilter}`;

    try {
        const response = await api.get(endpoint);
        if (response.success) renderTokenTracker(response.data);
    } catch (e) {
        console.error('Failed to load token tracker:', e);
    }
}

function renderTokenTracker(tokens) {
    const tbody = document.querySelector('#token-tracker-table tbody');

    if (!tokens || tokens.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">No tokens tracked yet</td></tr>';
        return;
    }

    tbody.innerHTML = tokens.map(token => {
        const symbol      = token.symbol || token.token_symbol || '—';
        const name        = token.name   || token.token_name   || symbol;
        const platform    = token.platform || token.source     || '?';
        const status      = token.status  || 'detected';
        const mint        = token.token_mint || '';

        const entryPrice  = token.entry_price_usd ?? token.price_usd ?? 0;
        const currentPrice = token.price_usd ?? 0;
        const changePct   = entryPrice > 0
            ? ((currentPrice - entryPrice) / entryPrice * 100)
            : 0;
        const changeClass = changePct > 0 ? 'text-success' : changePct < 0 ? 'text-danger' : 'text-muted';
        const changeStr   = entryPrice > 0
            ? `<span class="${changeClass}">${changePct >= 0 ? '+' : ''}${changePct.toFixed(1)}%</span>`
            : '<span class="text-muted">—</span>';

        const mc           = token.market_cap ?? 0;
        const failReason   = token.filter_fail_reason || '';
        const statusBadge  = `<span class="badge status-${status}">${status.replace(/_/g, ' ')}</span>`;

        const solscanUrl  = token.solscan_url     || null;
        const dexUrl      = token.dexscreener_url || null;
        const pumpUrl     = (platform === 'pumpfun' || platform === 'pump.fun') && mint
            ? `https://pump.fun/${mint}` : null;
        const links = [
            solscanUrl ? `<a href="${solscanUrl}" target="_blank" style="color:#58a6ff">Tx</a>` : '',
            dexUrl     ? `<a href="${dexUrl}"     target="_blank" style="color:#14f195">Chart</a>` : '',
            pumpUrl    ? `<a href="${pumpUrl}"    target="_blank" style="color:#ff6b35">Pump</a>` : '',
        ].filter(Boolean).join(' · ');

        const rowClick = mint ? `onclick="showTokenDetail('${mint}')" style="cursor:pointer"` : '';

        // ML score badge
        const mlScore = token.ml_score ?? null;
        const mlSignal = token.pump_signal ?? false;
        let mlBadge;
        if (mlScore === null || mlScore === undefined) {
            mlBadge = '<span class="text-muted">—</span>';
        } else {
            const mlColor = mlSignal ? '#14f195' : mlScore >= 15 ? '#fbbf24' : '#8b949e';
            const mlIcon  = mlSignal ? ' ▲' : '';
            mlBadge = `<span style="color:${mlColor};font-weight:600;" title="ML pump probability: ${(token.pump_probability??0)*100|0}%">${mlScore}${mlIcon}</span>`;
        }

        return `<tr ${rowClick}>
            <td>
                <strong>${symbol}</strong><br>
                <small class="text-muted">${name}</small><br>
                <small>${links}</small>
            </td>
            <td><span class="badge">${platform}</span></td>
            <td>${statusBadge}</td>
            <td>${formatPrice(entryPrice)}</td>
            <td>${formatPrice(currentPrice)}</td>
            <td>${changeStr}</td>
            <td>$${Number(mc).toLocaleString(undefined, {maximumFractionDigits: 0})}</td>
            <td>${mlBadge}</td>
            <td><small class="text-muted">${failReason || '—'}</small></td>
            <td>${formatRelativeTime(token.detected_at)}</td>
        </tr>`;
    }).join('');
}

async function showTokenDetail(mint) {
    try {
        const resp = await api.get(`/sniper/token/${mint}`);
        if (!resp.success) return;
        const token = resp.data;

        const panel = document.getElementById('token-detail-panel');
        panel.style.display = '';
        document.getElementById('token-detail-title').textContent =
            `${token.symbol || token.token_symbol || '?'} — ${token.name || token.token_name || ''}`;

        // Info grid
        const entryPrice   = token.entry_price_usd ?? token.price_usd ?? 0;
        const currentPrice = token.price_usd ?? 0;
        const changePct    = entryPrice > 0
            ? ((currentPrice - entryPrice) / entryPrice * 100).toFixed(2) : '—';

        document.getElementById('token-detail-info').innerHTML = [
            ['Mint',         `<small>${(token.token_mint||'').slice(0,16)}…</small>`],
            ['Platform',     token.platform || '?'],
            ['Status',       token.status   || '?'],
            ['Entry Price',  formatPrice(entryPrice)],
            ['Current Price',formatPrice(currentPrice)],
            ['Change',       changePct !== '—' ? `${changePct}%` : '—'],
            ['Market Cap',   `$${Number(token.market_cap||0).toLocaleString(undefined,{maximumFractionDigits:0})}`],
            ['Liquidity',    `$${Number(token.liquidity||0).toLocaleString(undefined,{maximumFractionDigits:0})}`],
            ['Risk Score',   `${token.risk_score ?? 100}/100`],
            ['Detected',     formatRelativeTime(token.detected_at)],
        ].map(([k,v]) =>
            `<div style="background:var(--bg-secondary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">${k}</div>
                <div style="font-weight:600;">${v}</div>
            </div>`
        ).join('');

        // Price chart
        renderPriceChart(token.price_snapshots || []);
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
        console.error('Failed to load token detail:', e);
    }
}

function closeTokenDetail() {
    document.getElementById('token-detail-panel').style.display = 'none';
}

// ── Simulation P&L ───────────────────────────────────────────────────────────

let _simFilter = 'open';

function setSimFilter(filter, btn) {
    _simFilter = filter;
    document.querySelectorAll('.sim-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    loadSimulation();
}

async function resetSimulation() {
    const confirmed = await showConfirm(
        'Reset all simulation positions? This cannot be undone.',
        'Reset',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await api.post('/sniper/simulation/reset');
        if (response.success) {
            showSuccess('Simulation reset successfully');
            loadSimulation();
        } else {
            showError('Failed to reset simulation');
        }
    } catch (e) {
        showError('Failed to reset simulation');
    }
}

/**
 * Called by the Refresh button — forces an immediate backend price update for
 * open positions, then re-renders stats + positions with the fresh data.
 */
async function refreshSimulationNow(btn) {
    const original = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    try {
        const resp = await api.post('/sniper/simulation/refresh-prices');
        if (resp.success) {
            renderSimStats(resp.data.stats);
            const positions = _simFilter === 'all'
                ? resp.data.positions
                : resp.data.positions.filter(p => p.status === _simFilter);
            renderSimPositions(positions);
        }
    } catch (e) {
        console.error('Failed to refresh simulation prices:', e);
        showError('Failed to refresh simulation');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = original; }
    }
}

async function loadSimulation() {
    try {
        const [statsResp, posResp] = await Promise.all([
            api.get('/sniper/simulation/stats'),
            api.get(_simFilter === 'all'
                ? '/sniper/simulation/positions?limit=200'
                : `/sniper/simulation/positions?status=${_simFilter}&limit=200`)
        ]);
        if (statsResp.success) renderSimStats(statsResp.data);
        if (posResp.success)   renderSimPositions(posResp.data);
    } catch (e) {
        console.error('Failed to load simulation data:', e);
    }
}

function renderSimStats(s) {
    const pnlColor = v => v > 0 ? '#14f195' : v < 0 ? '#f85149' : '#8b949e';
    const solStr   = v => `${v >= 0 ? '+' : ''}${v.toFixed(4)} SOL`;
    const pctStr   = v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;

    document.getElementById('sim-total-trades').textContent    = s.total_trades;
    document.getElementById('sim-open-count').textContent      = s.open_count;
    document.getElementById('sim-win-rate').textContent        = s.closed_count > 0 ? `${s.win_rate}%` : '—';
    document.getElementById('sim-realized-pnl').innerHTML      = `<span style="color:${pnlColor(s.realized_pnl_sol)}">${solStr(s.realized_pnl_sol)}</span>`;
    document.getElementById('sim-unrealized-pnl').innerHTML    = `<span style="color:${pnlColor(s.unrealized_pnl_sol)}">${solStr(s.unrealized_pnl_sol)}</span>`;
    document.getElementById('sim-total-pnl').innerHTML         = `<span style="color:${pnlColor(s.total_pnl_sol)}">${solStr(s.total_pnl_sol)}</span>`;
    document.getElementById('sim-roi').innerHTML               = `<span style="color:${pnlColor(s.roi_percent)}">${pctStr(s.roi_percent)}</span>`;
    document.getElementById('sim-best-trade').innerHTML        = `<span style="color:#14f195">${pctStr(s.best_trade_pct)}</span>`;
    document.getElementById('sim-worst-trade').innerHTML       = `<span style="color:#f85149">${pctStr(s.worst_trade_pct)}</span>`;
}

function renderSimPositions(positions) {
    const tbody = document.querySelector('#sim-positions-table tbody');

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No positions match this filter</td></tr>';
        return;
    }

    tbody.innerHTML = positions.map(p => {
        const symbol   = p.token_symbol || p.symbol || '—';
        const status   = p.status || 'open';
        const change   = p.pnl_percent ?? 0;
        const pnlSol   = p.pnl_sol ?? 0;
        const chColor  = change > 0 ? '#14f195' : change < 0 ? '#f85149' : '#8b949e';
        const pnlColor = pnlSol > 0 ? '#14f195' : pnlSol < 0 ? '#f85149' : '#8b949e';

        const statusMap = {
            open:          '<span class="badge status-open">Open</span>',
            closed_tp:     '<span class="badge status-tp">TP ✓</span>',
            closed_sl:     '<span class="badge status-sl">SL ✗</span>',
            closed_manual: '<span class="badge">Closed</span>',
        };

        const exitOrCurrent = status === 'open'
            ? formatPrice(p.current_price ?? p.entry_price)
            : formatPrice(p.exit_price ?? 0);

        const dexUrl  = p.dexscreener_url || null;
        const pumpUrl = p.platform === 'pumpfun' && p.token_mint
            ? `https://pump.fun/${p.token_mint}` : null;
        const links   = [
            dexUrl  ? `<a href="${dexUrl}"  target="_blank" style="color:#14f195">Chart</a>` : '',
            pumpUrl ? `<a href="${pumpUrl}" target="_blank" style="color:#ff6b35">Pump</a>`  : '',
        ].filter(Boolean).join(' · ');

        const feesSol   = p.fees_sol ?? p.fee_entry_sol ?? 0;
        const netSol    = p.simulated_sol_net ?? (p.simulated_sol ?? 0.1);
        const feesTooltip = `Entry: ${(p.fee_entry_pct??1.25).toFixed(2)}% / Exit: ${(p.fee_exit_pct??1.25).toFixed(2)}%`;

        // ML score badge for simulation position
        const simMlScore  = p.ml_score ?? null;
        const simMlSignal = p.pump_signal ?? false;
        let simMlBadge;
        if (simMlScore === null || simMlScore === undefined) {
            simMlBadge = '<span class="text-muted">—</span>';
        } else {
            const mlColor = simMlSignal ? '#14f195' : simMlScore >= 15 ? '#fbbf24' : '#8b949e';
            const mlIcon  = simMlSignal ? ' ▲' : '';
            simMlBadge = `<span style="color:${mlColor};font-weight:600;" title="ML pump probability at entry: ${(p.pump_probability??0)*100|0}%">${simMlScore}${mlIcon}</span>`;
        }

        return `<tr>
            <td>
                <strong>${symbol}</strong><br>
                <small class="text-muted">${p.name || ''}</small><br>
                <small>${links}</small>
            </td>
            <td>${statusMap[status] || `<span class="badge">${status}</span>`}</td>
            <td>${formatPrice(p.entry_price)}</td>
            <td>${exitOrCurrent}</td>
            <td><span style="color:${chColor};font-weight:600;">${change >= 0 ? '+' : ''}${change.toFixed(1)}%</span></td>
            <td><span style="color:${pnlColor};font-weight:600;" title="Net after fees">${pnlSol >= 0 ? '+' : ''}${pnlSol.toFixed(4)}</span></td>
            <td><span class="text-muted" title="${feesTooltip}">-${feesSol.toFixed(4)}</span></td>
            <td>${netSol.toFixed(3)} SOL</td>
            <td>$${Number(p.entry_mc || 0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
            <td>${simMlBadge}</td>
            <td>${formatRelativeTime(p.entry_time)}</td>
        </tr>`;
    }).join('');
}

let _priceChart = null;

function renderPriceChart(snapshots) {
    const canvas = document.getElementById('token-price-chart');
    if (!canvas) return;

    const labels = snapshots.map(s => {
        const d = new Date(s.ts);
        return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
    });
    const prices = snapshots.map(s => s.price_usd || 0);
    const mcs    = snapshots.map(s => s.market_cap || 0);

    if (_priceChart) { _priceChart.destroy(); _priceChart = null; }

    if (typeof Chart === 'undefined' || snapshots.length === 0) {
        canvas.style.display = 'none';
        return;
    }
    canvas.style.display = '';

    _priceChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Price (USD)',
                data: prices,
                borderColor: '#14f195',
                backgroundColor: 'rgba(20,241,149,0.08)',
                fill: true,
                tension: 0.3,
                pointRadius: snapshots.length > 30 ? 0 : 3,
            }]
        },
        options: {
            responsive: true,
            scales: {
                x: { ticks: { maxTicksLimit: 8, color: '#8b949e' }, grid: { color: '#21262d' } },
                y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } }
            },
            plugins: { legend: { labels: { color: '#e6edf3' } } }
        }
    });
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Format a token price using DexScreener-style subscript notation for very
 * small values, e.g. 0.000002328 → $0.0₅2328
 *
 * Returns an HTML string (use innerHTML, not textContent).
 */
function formatPrice(price) {
    if (!price || price === 0) return '$0';

    // Normal ranges — no subscript needed
    if (price >= 1)     return `$${price.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    if (price >= 0.01)  return `$${price.toFixed(4)}`;
    if (price >= 0.001) return `$${price.toFixed(6)}`;

    // Very small: count leading zeros after the decimal point
    // e.g. 0.000002328 → zeros=5, sig="2328" → $0.0<sub>5</sub>2328
    const s = price.toFixed(12).replace(/0+$/, '');   // "0.000002328"
    const m = s.match(/^0\.(0*)([1-9]\d{0,3})/);      // capture zeros + up to 4 sig digits
    if (m) {
        const zeros = m[1].length;   // number of leading zeros after "0."
        const sig   = m[2];          // significant digits (up to 4)
        if (zeros >= 2) {
            return `$0.0<sub>${zeros}</sub>${sig}`;
        }
    }
    return `$${price.toFixed(8)}`;
}

function renderDetectedTokens(tokens, showAll = false) {
    const tbody = document.querySelector('#detected-tokens-table tbody');

    tokens = tokens || [];

    if (tokens.length === 0) {
        const msg = showAll ? 'No tokens detected yet' : 'No tokens currently being watched';
        tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">${msg}</td></tr>`;
        return;
    }

    const tokensHTML = tokens.map(token => {
        const symbol      = token.symbol      || token.token_symbol || '—';
        const name        = token.name        || token.token_name   || symbol;
        const platform    = token.platform    || token.source       || 'unknown';
        const liquidity   = token.liquidity   ?? token.initial_liquidity ?? 0;
        const marketCap   = token.market_cap  ?? 0;
        const riskScore   = token.risk_score  ?? 100;
        const status      = token.status      || 'detected';
        const actionTaken = token.action_taken || 'pending';
        const solscanUrl     = token.solscan_url     || null;
        const dexscreenerUrl = token.dexscreener_url || null;
        // Build a pump.fun link for pumpfun-sourced tokens using the mint address
        const tokenMint      = token.token_mint      || null;
        const pumpfunUrl     = (token.platform === 'pumpfun' && tokenMint)
            ? `https://pump.fun/${tokenMint}` : null;
        const links = [
            solscanUrl     ? `<span style="cursor:pointer;color:#58a6ff;" onclick="window.open('${solscanUrl}','_blank')" title="Solscan">Tx</span>`            : '',
            dexscreenerUrl ? `<span style="cursor:pointer;color:#14f195;" onclick="window.open('${dexscreenerUrl}','_blank')" title="DexScreener">Chart</span>` : '',
            pumpfunUrl     ? `<span style="cursor:pointer;color:#ff6b35;" onclick="window.open('${pumpfunUrl}','_blank')" title="Pump.fun">Pump</span>`          : '',
        ].filter(Boolean).join(' · ');
        const nameDisplay = name + (links ? `<br><small>${links}</small>` : '');
        return `
        <tr>
            <td>
                <strong>${symbol}</strong><br>
                <small class="text-muted">${nameDisplay}</small>
            </td>
            <td><span class="badge">${platform}</span></td>
            <td>${Number(liquidity).toLocaleString()}</td>
            <td>${Number(marketCap).toLocaleString()}</td>
            <td>
                <span class="${getRiskScoreClass(riskScore)}">${riskScore}/100</span>
            </td>
            <td><span class="badge ${status}">${status.replace(/_/g, ' ')}</span></td>
            <td><span class="badge">${actionTaken.replace(/_/g, ' ')}</span></td>
            <td>${formatRelativeTime(token.detected_at)}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = tokensHTML;
}

function getRiskScoreClass(score) {
    if (score >= 70) return 'text-success';
    if (score >= 50) return 'text-warning';
    return 'text-danger';
}

async function startSniper() {
    // Check if WebSocket is running first
    const wsReady = await checkWebSocketRequired('Sniper Bot');
    if (!wsReady) {
        return; // User cancelled or WebSocket couldn't start
    }

    showLoading('Starting Sniper Bot...');

    try {
        await api.startSniper();

        // Wait for subscriptions to be confirmed by the server
        await new Promise(resolve => setTimeout(resolve, 2000));

        await Promise.all([loadSniperData(), updateWebSocketStatus()]);
        hideLoading();
        showSuccess('Sniper Bot started successfully');
    } catch (error) {
        hideLoading();
        showError('Failed to start sniper bot');
    }
}

async function stopSniper() {
    try {
        await api.stopSniper();
        showSuccess('Sniper bot stopped');
        await Promise.all([loadSniperData(), updateWebSocketStatus()]);
    } catch (error) {
        showError('Failed to stop sniper bot');
    }
}

function showSniperConfigModal() {
    // Load current config first
    api.getSniperConfig().then(response => {
        if (response.success) {
            const config = response.data;

            const content = `
                <form onsubmit="saveSniperConfigHandler(event)">
                    <div class="form-group">
                        <label class="form-label">Mode</label>
                        <select id="sniper-mode-select">
                            <option value="notification" ${config.mode === 'notification' ? 'selected' : ''}>Notification - Only notify</option>
                            <option value="simulation" ${config.mode === 'simulation' ? 'selected' : ''}>Simulation - Virtual execution</option>
                            <option value="auto_buy" ${config.mode === 'auto_buy' ? 'selected' : ''}>Auto Buy - Execute automatically</option>
                        </select>
                    </div>

                    <h4>Filters</h4>
                    <div class="form-grid">
                        <div class="form-group">
                            <label class="form-label">Min Liquidity</label>
                            <input type="number" id="sniper-min-liquidity" value="${config.filters?.min_liquidity ?? 1000}">
                        </div>

                        <div class="form-group">
                            <label class="form-label">Max Market Cap</label>
                            <input type="number" id="sniper-max-market-cap" value="${config.filters?.max_market_cap ?? 100000}">
                        </div>
                    </div>

                    <div class="form-grid">
                        <div class="form-group">
                            <label class="form-label">Min Initial Volume</label>
                            <input type="number" id="sniper-min-volume" value="${config.filters?.min_initial_volume ?? 500}">
                        </div>

                        <div class="form-group">
                            <label class="form-label">Max Time Since Creation (min)</label>
                            <input type="number" id="sniper-max-time" value="${config.filters?.max_time_since_creation_minutes || 5}">
                        </div>
                    </div>

                    <h4>Platforms to Monitor</h4>
                    <div class="form-group">
                        <label class="checkbox">
                            <input type="checkbox" id="sniper-platform-pumpfun"
                                ${(config.filters?.platforms || []).includes('pump.fun') ? 'checked' : ''}>
                            <span>Pump.fun</span>
                        </label>
                    </div>
                    <div class="form-group">
                        <label class="checkbox">
                            <input type="checkbox" id="sniper-platform-raydium"
                                ${(config.filters?.platforms || []).includes('raydium') ? 'checked' : ''}>
                            <span>Raydium</span>
                        </label>
                    </div>

                    <div class="form-group">
                        <label class="checkbox">
                            <input type="checkbox" id="sniper-anti-scam-enabled" ${config.anti_scam?.enabled ? 'checked' : ''}>
                            <span>Enable Anti-Scam Analysis</span>
                        </label>
                    </div>

                    <h4>Execution Settings</h4>
                    <div class="form-grid">
                        <div class="form-group">
                            <label class="form-label">Auto Buy Amount (SOL)</label>
                            <input type="number" step="0.01" id="sniper-auto-buy-amount" value="${config.execution?.auto_buy_amount || 0.1}">
                        </div>

                        <div class="form-group">
                            <label class="form-label">Max Slippage (%)</label>
                            <input type="number" step="0.1" id="sniper-max-slippage" value="${config.execution?.max_slippage || 10}">
                        </div>
                    </div>

                    <h4>TP / SL / Fees</h4>
                    <div class="form-grid">
                        <div class="form-group">
                            <label class="form-label">Take Profit (%)</label>
                            <input type="number" step="1" min="1" id="sniper-tp-percent"
                                value="${config.simulation?.tp_percent ?? 50}"
                                placeholder="e.g. 50 = close at +50%">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Stop Loss (%)</label>
                            <input type="number" step="1" min="1" id="sniper-sl-percent"
                                value="${config.simulation?.sl_percent ?? 30}"
                                placeholder="e.g. 30 = close at -30%">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Entry Fee (%) <small class="text-muted">Pump.fun 1% + slippage</small></label>
                            <input type="number" step="0.05" min="0" id="sniper-fee-entry"
                                value="${config.simulation?.fee_entry_percent ?? 1.25}">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Exit Fee (%) <small class="text-muted">same on sale</small></label>
                            <input type="number" step="0.05" min="0" id="sniper-fee-exit"
                                value="${config.simulation?.fee_exit_percent ?? 1.25}">
                        </div>
                    </div>

                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                        <button type="submit" class="btn btn-primary">Save Configuration</button>
                    </div>
                </form>
            `;

            showModal('Sniper Configuration', content);
        }
    }).catch(error => {
        showError('Failed to load sniper configuration');
    });
}

async function saveSniperConfigHandler(event) {
    event.preventDefault();

    const platforms = [];
    if (document.getElementById('sniper-platform-pumpfun').checked) platforms.push('pump.fun');
    if (document.getElementById('sniper-platform-raydium').checked) platforms.push('raydium');

    if (platforms.length === 0) {
        showError('Select at least one platform to monitor.');
        return;
    }

    const configData = {
        mode: document.getElementById('sniper-mode-select').value,
        filters: {
            min_liquidity: parseInt(document.getElementById('sniper-min-liquidity').value),
            max_market_cap: parseInt(document.getElementById('sniper-max-market-cap').value),
            min_initial_volume: parseInt(document.getElementById('sniper-min-volume').value),
            max_time_since_creation_minutes: parseInt(document.getElementById('sniper-max-time').value),
            platforms,
        },
        execution: {
            auto_buy_amount: parseFloat(document.getElementById('sniper-auto-buy-amount').value),
            max_slippage: parseFloat(document.getElementById('sniper-max-slippage').value)
        },
        simulation: {
            tp_percent:        parseFloat(document.getElementById('sniper-tp-percent').value) || 50,
            sl_percent:        parseFloat(document.getElementById('sniper-sl-percent').value) || 30,
            fee_entry_percent: parseFloat(document.getElementById('sniper-fee-entry').value) ?? 1.25,
            fee_exit_percent:  parseFloat(document.getElementById('sniper-fee-exit').value)  ?? 1.25,
        },
        anti_scam: {
            enabled: document.getElementById('sniper-anti-scam-enabled').checked
        }
    };

    try {
        const response = await api.updateSniperConfig(configData);

        if (response.success) {
            closeModal();
            showSuccess('Sniper configuration saved successfully');
            await loadSniperData();
        }
    } catch (error) {
        console.error('Failed to save sniper configuration:', error);
        showError('Failed to save sniper configuration');
    }
}
