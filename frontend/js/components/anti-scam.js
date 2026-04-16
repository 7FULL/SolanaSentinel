/**
 * Anti-Scam Analyzer Component
 */

async function analyzeToken(event) {
    event.preventDefault();

    const tokenAddress = document.getElementById('token-address-input').value;
    const resultContainer = document.getElementById('analysis-result');

    if (!tokenAddress) {
        showError('Please enter a token address');
        return;
    }

    resultContainer.innerHTML = '';
    showLoading('Analyzing token on-chain data...\nThis may take a few seconds.');

    try {
        const response = await api.analyzeToken(tokenAddress);

        hideLoading();

        if (response.success) {
            renderAnalysisResult(response.data);
        }
    } catch (error) {
        hideLoading();
        resultContainer.innerHTML = '<p class="text-danger">Failed to analyze token</p>';
        showError('Failed to analyze token: ' + error.message);
    }
}

function renderAnalysisResult(analysis) {
    const container = document.getElementById('analysis-result');

    const checksHTML = Object.entries(analysis.checks).map(([key, check]) => `
        <div class="module-item">
            <span class="module-name">${formatCheckName(key)}</span>
            <span class="module-status-badge ${check.passed ? 'active' : 'inactive'}">
                ${check.score}/${check.max_score} - ${check.message}
            </span>
        </div>
    `).join('');

    const warningsHTML = analysis.warnings.length > 0 ?
        '<div class="alert alert-warning">⚠️ ' + analysis.warnings.join('<br>') + '</div>' : '';

    const redFlagsHTML = analysis.red_flags.length > 0 ?
        '<div class="alert alert-danger">🚨 ' + analysis.red_flags.join('<br>') + '</div>' : '';

    container.innerHTML = `
        <div class="card">
            <div class="card-body">
                <div class="stats-grid" style="grid-template-columns: repeat(3, 1fr); margin-bottom: 20px;">
                    <div>
                        <strong>Risk Score</strong><br>
                        <span class="${getRiskScoreClass(analysis.risk_score)}" style="font-size: 2rem;">
                            ${analysis.risk_score}/100
                        </span>
                    </div>
                    <div>
                        <strong>Risk Level</strong><br>
                        <span class="badge ${analysis.risk_level}">${analysis.risk_level}</span>
                    </div>
                    <div>
                        <strong>Passed</strong><br>
                        <span class="${analysis.passed ? 'text-success' : 'text-danger'}">
                            ${analysis.passed ? '✓ Yes' : '✗ No'}
                        </span>
                    </div>
                </div>

                ${warningsHTML}
                ${redFlagsHTML}

                <h4>Security Checks</h4>
                <div class="module-status-grid">${checksHTML}</div>

                <div style="margin-top: 20px;">
                    <strong>Recommendation:</strong>
                    <p>${analysis.recommendation}</p>
                </div>

                <div style="margin-top: 10px; font-size: 0.75rem; color: var(--text-muted);">
                    Analyzed at: ${formatTimestamp(analysis.analyzed_at)}
                </div>
            </div>
        </div>
    `;
}

function formatCheckName(name) {
    return name.split('_').map(word =>
        word.charAt(0).toUpperCase() + word.slice(1)
    ).join(' ');
}

async function loadBlacklist() {
    try {
        const response = await api.getBlacklist();

        if (response.success) {
            renderBlacklist(response.data);
        }
    } catch (error) {
        console.error('Failed to load blacklist:', error);
    }
}

function renderBlacklist(blacklist) {
    const container = document.getElementById('blacklist-content');

    const tokensCount = Object.keys(blacklist.tokens || {}).length;
    const walletsCount = Object.keys(blacklist.wallets || {}).length;

    if (tokensCount === 0 && walletsCount === 0) {
        container.innerHTML = '<p class="text-muted">No blacklisted items</p>';
        return;
    }

    const tokensHTML = Object.entries(blacklist.tokens || {}).map(([address, data]) => `
        <div class="module-item">
            <div>
                <div class="address-short">${shortenAddress(address)}</div>
                <small class="text-muted">${data.reason}</small>
            </div>
            <button class="btn btn-sm btn-danger" onclick="removeFromBlacklistHandler('${address}')">Remove</button>
        </div>
    `).join('');

    const walletsHTML = Object.entries(blacklist.wallets || {}).map(([address, data]) => `
        <div class="module-item">
            <div>
                <div class="address-short">${shortenAddress(address)}</div>
                <small class="text-muted">${data.reason}</small>
            </div>
            <button class="btn btn-sm btn-danger" onclick="removeFromBlacklistHandler('${address}')">Remove</button>
        </div>
    `).join('');

    container.innerHTML = `
        ${tokensCount > 0 ? `<h4>Blacklisted Tokens (${tokensCount})</h4>${tokensHTML}` : ''}
        ${walletsCount > 0 ? `<h4 style="margin-top: 20px;">Blacklisted Wallets (${walletsCount})</h4>${walletsHTML}` : ''}
    `;
}

async function removeFromBlacklistHandler(address) {
    const confirmed = await showConfirm(
        'Are you sure you want to remove this address from the blacklist?',
        'Remove',
        'Cancel'
    );

    if (!confirmed) return;

    try {
        await api.removeFromBlacklist(address);
        await loadBlacklist();
        showSuccess('Removed from blacklist');
    } catch (error) {
        showError('Failed to remove from blacklist');
    }
}

// ---------------------------------------------------------------
// Configuration panel
// ---------------------------------------------------------------

const _CHECK_META = {
    lp_locked:                     { label: 'LP Locked',             desc: 'Liquidity locked or burned. pump.fun: still on bonding curve or burned on migration.' },
    mint_disabled:                 { label: 'Mint Disabled',         desc: 'No new tokens can be minted after launch.' },
    freeze_disabled:               { label: 'Freeze Disabled',       desc: 'Creator cannot freeze token holder wallets.' },
    max_creator_percentage:        { label: 'Max Creator Holding',   desc: 'Creator wallet holds no more than X% of supply.' },
    min_holders:                   { label: 'Min Holders',           desc: 'Token has at least N distinct holder wallets.' },
    max_top_10_holders_percentage: { label: 'Top 10 Holders Cap',    desc: 'Top 10 wallets own less than X% of supply.' },
};

async function loadAntiScamConfig() {
    try {
        const response = await api.getAntiScamConfig();
        if (response.success) {
            renderAntiScamConfig(response.data);
        }
    } catch (error) {
        console.error('Failed to load anti-scam config:', error);
        const body = document.getElementById('anti-scam-config-body');
        if (body) body.innerHTML = '<p class="text-danger">Failed to load configuration</p>';
    }
}

function renderAntiScamConfig(cfg) {
    const body = document.getElementById('anti-scam-config-body');
    if (!body) return;

    const checks   = cfg.checks || {};
    const enabled  = cfg.enabled !== false;
    const maxScore = cfg.max_risk_score || 70;

    const checksRows = Object.entries(checks).map(([key, chk]) => {
        const meta         = _CHECK_META[key] || { label: key, desc: '' };
        const hasThreshold = 'threshold' in chk;
        return `
            <div class="anti-scam-check-row">
                <div class="anti-scam-check-info">
                    <span class="anti-scam-check-label">${meta.label}</span>
                    <span class="anti-scam-check-desc">${meta.desc}</span>
                </div>
                <div class="anti-scam-check-controls">
                    <label class="toggle-label">
                        <input type="checkbox" class="as-check-enabled" data-key="${key}"
                            ${chk.enabled !== false ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                    <label class="anti-scam-field-label">
                        Penalty
                        <input type="number" class="form-control form-control-sm as-penalty"
                            data-key="${key}" value="${chk.penalty || 0}" min="0" max="100"
                            style="width:70px">
                    </label>
                    ${hasThreshold ? `
                    <label class="anti-scam-field-label">
                        Threshold
                        <input type="number" class="form-control form-control-sm as-threshold"
                            data-key="${key}" value="${chk.threshold}" min="0"
                            style="width:80px">
                    </label>` : ''}
                </div>
            </div>
        `;
    }).join('');

    body.innerHTML = `
        <form onsubmit="saveAntiScamConfig(event)">
            <div class="anti-scam-module-row">
                <label class="toggle-label">
                    <input type="checkbox" id="as-module-enabled" ${enabled ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                    <span style="margin-left:8px">Module enabled</span>
                </label>
                <label class="anti-scam-field-label" style="margin-left:auto">
                    Max risk score
                    <input type="number" id="as-max-risk-score" class="form-control form-control-sm"
                        value="${maxScore}" min="0" max="100" style="width:80px">
                </label>
            </div>
            <div class="anti-scam-checks-grid">
                ${checksRows}
            </div>
            <div style="margin-top: var(--spacing-md); text-align: right;">
                <button type="submit" class="btn btn-primary btn-sm">Save Configuration</button>
            </div>
        </form>
    `;

    _addAntiScamConfigStyles();
}

async function saveAntiScamConfig(event) {
    event.preventDefault();

    const enabled  = document.getElementById('as-module-enabled').checked;
    const maxScore = parseInt(document.getElementById('as-max-risk-score').value, 10);
    const checks   = {};

    document.querySelectorAll('.as-check-enabled').forEach(el => {
        const key = el.dataset.key;
        checks[key] = { enabled: el.checked };
    });

    document.querySelectorAll('.as-penalty').forEach(el => {
        const key = el.dataset.key;
        if (checks[key]) checks[key].penalty = parseInt(el.value, 10) || 0;
    });

    document.querySelectorAll('.as-threshold').forEach(el => {
        const key = el.dataset.key;
        if (checks[key]) checks[key].threshold = parseFloat(el.value) || 0;
    });

    try {
        const response = await api.updateAntiScamConfig({ enabled, max_risk_score: maxScore, checks });
        if (response.success) {
            showSuccess('Anti-scam configuration saved');
        }
    } catch (error) {
        showError('Failed to save anti-scam configuration');
    }
}

// ── ML Pump Predictor card ────────────────────────────────────────────────────

// Polling interval while training is in progress (ms)
const ML_POLL_INTERVAL = 5000;
let _mlPollTimer = null;

async function loadMLConfig() {
    const body = document.getElementById('ml-config-body');
    if (!body) return;
    try {
        const [statusResp, cfgResp, trainerResp] = await Promise.all([
            api.getAIStatus(),
            api.getSniperMLConfig(),
            api.getRetrainStatus(),
        ]);
        if (statusResp.success && cfgResp.success) {
            const trainerStatus = trainerResp.success ? trainerResp.data : null;
            renderMLConfig(statusResp.data, cfgResp.data, trainerStatus);
        } else {
            body.innerHTML = '<p class="text-danger">Failed to load ML config</p>';
        }
    } catch (e) {
        console.error('Failed to load ML config:', e);
        body.innerHTML = '<p class="text-danger">Failed to load ML config</p>';
    }
}

function renderMLConfig(status, mlCfg, trainerStatus) {
    const body = document.getElementById('ml-config-body');
    if (!body) return;

    const ml        = status.ml_model || {};
    const loaded    = ml.loaded === true;
    const enabled   = mlCfg.enabled === true;
    const minScore  = mlCfg.min_pump_score ?? 0;
    const training  = trainerStatus?.is_training === true;
    const lastTrain = trainerStatus?.last_trained
        ? new Date(trainerStatus.last_trained).toLocaleString()
        : null;
    const lastResult = trainerStatus?.last_result || null;

    // Model status dot
    const statusDot = loaded
        ? '<span style="color:#14f195;font-size:1.1rem;">●</span> Loaded'
        : '<span style="color:#f85149;font-size:1.1rem;">●</span> Not loaded';

    // Model metrics grid
    const modelStats = loaded ? `
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin:12px 0;">
            <div style="background:var(--bg-tertiary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Model</div>
                <div style="font-weight:600;">${ml.name || '—'}</div>
            </div>
            <div style="background:var(--bg-tertiary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">ROC-AUC</div>
                <div style="font-weight:600;">${ml.roc_auc != null ? ml.roc_auc.toFixed(3) : '—'}</div>
            </div>
            <div style="background:var(--bg-tertiary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">PR-AUC</div>
                <div style="font-weight:600;">${ml.pr_auc != null ? ml.pr_auc.toFixed(3) : '—'}</div>
            </div>
            <div style="background:var(--bg-tertiary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Threshold</div>
                <div style="font-weight:600;">${ml.threshold != null ? ml.threshold.toFixed(3) : '—'}</div>
            </div>
            <div style="background:var(--bg-tertiary);padding:10px;border-radius:6px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Base Rate</div>
                <div style="font-weight:600;">${ml.base_rate != null ? (ml.base_rate * 100).toFixed(1) + '%' : '—'}</div>
            </div>
        </div>`
        : '<p class="text-muted" style="margin:12px 0;">No model loaded. Use the Retrain button or run notebooks/02_train.ipynb.</p>';

    // Last training info row
    const trainInfo = lastTrain
        ? `<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">
               Last trained: <strong style="color:var(--text-secondary);">${lastTrain}</strong>
               ${lastResult && lastResult.success ? `&nbsp;—&nbsp;PR-AUC <strong>${(lastResult.pr_auc || 0).toFixed(3)}</strong>  lift <strong>${lastResult.lift || '—'}x</strong>` : ''}
               ${lastResult && !lastResult.success ? `&nbsp;<span style="color:#f85149;">Failed: ${lastResult.error || 'unknown error'}</span>` : ''}
           </div>`
        : '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">Never retrained automatically.</div>';

    // Retrain button / spinner
    const retrainBtn = training
        ? `<button class="btn btn-sm" disabled style="display:inline-flex;align-items:center;gap:6px;">
               <span class="spinner" style="width:12px;height:12px;border-width:2px;"></span>
               Training...
           </button>`
        : `<button class="btn btn-sm btn-outline" onclick="triggerRetrain()" type="button"
               title="Run Optuna RF tuning on all collected data now">
               Retrain Now
           </button>`;

    body.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px;">
            <div>${statusDot}</div>
            <div style="display:flex;align-items:center;gap:8px;">
                <span style="font-size:11px;color:var(--text-muted);">Runs weekly automatically</span>
                ${retrainBtn}
            </div>
        </div>
        ${trainInfo}
        ${modelStats}
        <form onsubmit="saveMLConfig(event)" style="border-top:1px solid var(--border-color);padding-top:14px;margin-top:4px;">
            <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
                <label class="toggle-label" style="gap:8px;">
                    <input type="checkbox" id="ml-enabled" ${enabled ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                    <span style="margin-left:4px;">Enable ML filter</span>
                </label>
                <label style="display:flex;align-items:center;gap:8px;font-size:0.85rem;color:var(--text-secondary);">
                    Min pump score (0–100)
                    <input type="number" id="ml-min-score" class="form-control form-control-sm"
                        min="0" max="100" step="1" value="${minScore}"
                        style="width:70px;" ${!enabled ? 'disabled' : ''}>
                </label>
                <small class="text-muted">When enabled, tokens scoring below the minimum are rejected by the sniper.</small>
            </div>
            <button type="submit" class="btn btn-primary" style="margin-top:12px;">Save</button>
        </form>`;

    // Toggle min-score input disabled state with the checkbox
    const cb    = body.querySelector('#ml-enabled');
    const input = body.querySelector('#ml-min-score');
    if (cb && input) {
        cb.addEventListener('change', () => { input.disabled = !cb.checked; });
    }

    // Start or stop polling based on whether training is active
    _mlStartPolling(training);
}

async function saveMLConfig(event) {
    event.preventDefault();
    const enabled  = document.getElementById('ml-enabled').checked;
    const minScore = parseInt(document.getElementById('ml-min-score').value) || 0;
    try {
        const resp = await api.updateSniperMLConfig({ enabled, min_pump_score: minScore });
        if (resp.success) {
            showSuccess('ML configuration saved');
        } else {
            showError('Failed to save ML configuration');
        }
    } catch (e) {
        showError('Failed to save ML configuration');
    }
}

async function triggerRetrain() {
    try {
        const resp = await api.triggerRetrain();
        if (resp.success) {
            showSuccess('Retraining started — this takes several minutes.');
            loadMLConfig(); // re-render to show spinner
        } else if (resp.status === 409) {
            showError('Training is already in progress.');
        } else {
            showError('Failed to start retraining: ' + (resp.message || 'unknown error'));
        }
    } catch (e) {
        showError('Failed to start retraining: ' + e.message);
    }
}

function _mlStartPolling(isTraining) {
    if (_mlPollTimer) {
        clearInterval(_mlPollTimer);
        _mlPollTimer = null;
    }
    if (!isTraining) return;

    // Poll every 5 seconds while training to update the spinner / results
    _mlPollTimer = setInterval(async () => {
        try {
            const resp = await api.getRetrainStatus();
            if (!resp.success) return;
            const still = resp.data.is_training;
            if (!still) {
                // Training done — reload full config to update model stats
                clearInterval(_mlPollTimer);
                _mlPollTimer = null;
                await loadMLConfig();
                showSuccess('Model retraining complete!');
            }
        } catch (_) { /* ignore poll errors */ }
    }, ML_POLL_INTERVAL);
}

function _addAntiScamConfigStyles() {
    if (document.getElementById('as-config-styles')) return;
    const style = document.createElement('style');
    style.id = 'as-config-styles';
    style.textContent = `
        .anti-scam-module-row {
            display: flex;
            align-items: center;
            padding-bottom: var(--spacing-md);
            margin-bottom: var(--spacing-md);
            border-bottom: 1px solid var(--border-color);
        }
        .anti-scam-checks-grid {
            display: flex;
            flex-direction: column;
            gap: var(--spacing-sm);
        }
        .anti-scam-check-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: var(--spacing-sm) var(--spacing-md);
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            gap: var(--spacing-md);
            flex-wrap: wrap;
        }
        .anti-scam-check-info {
            display: flex;
            flex-direction: column;
            flex: 1;
            min-width: 160px;
        }
        .anti-scam-check-label {
            font-weight: 600;
            font-size: 0.875rem;
        }
        .anti-scam-check-desc {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        .anti-scam-check-controls {
            display: flex;
            align-items: center;
            gap: var(--spacing-md);
            flex-wrap: wrap;
        }
        .anti-scam-field-label {
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            font-size: 0.8rem;
            color: var(--text-secondary);
        }
    `;
    document.head.appendChild(style);
}
