/**
 * Settings Component
 * Manages application-level configuration: RPC endpoint, WebSocket URL, network, commitment.
 */

// ---------------------------------------------------------------
// Section entry point
// ---------------------------------------------------------------

async function loadSettingsData() {
    await loadRpcSettings();
}

// ---------------------------------------------------------------
// RPC Settings
// ---------------------------------------------------------------

async function loadRpcSettings() {
    const body = document.getElementById('rpc-settings-body');
    if (!body) return;

    try {
        const resp = await api.getRpcSettings();
        if (resp.success) renderRpcSettings(resp.data);
    } catch (e) {
        body.innerHTML = '<p class="text-danger">Failed to load RPC settings.</p>';
    }
}

function renderRpcSettings(cfg) {
    const body = document.getElementById('rpc-settings-body');
    if (!body) return;

    // Detect provider from current URL so we can pre-fill the preset
    const currentUrl = cfg.rpc_url || '';
    let detectedPreset = 'custom';
    if (currentUrl.includes('helius-rpc.com'))   detectedPreset = 'helius';
    if (currentUrl.includes('devnet.solana.com')) detectedPreset = 'devnet';
    if (currentUrl.includes('mainnet-beta.solana.com')) detectedPreset = 'public-mainnet';

    body.innerHTML = `
        <form onsubmit="saveRpcSettings(event)">

            <!-- Quick-fill presets -->
            <div class="form-group">
                <label class="form-label">Quick Presets</label>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">
                    <button type="button" class="btn btn-sm ${detectedPreset==='helius'?'btn-primary':'btn-secondary'}"
                            onclick="applyRpcPreset('helius')">Helius (recommended)</button>
                    <button type="button" class="btn btn-sm ${detectedPreset==='public-mainnet'?'btn-primary':'btn-secondary'}"
                            onclick="applyRpcPreset('public-mainnet')">Public Mainnet</button>
                    <button type="button" class="btn btn-sm ${detectedPreset==='devnet'?'btn-primary':'btn-secondary'}"
                            onclick="applyRpcPreset('devnet')">Devnet</button>
                </div>
            </div>

            <!-- Helius API key helper (shown only for Helius preset) -->
            <div id="rpc-helius-helper" class="form-group" style="${detectedPreset==='helius'?'':'display:none'}">
                <label class="form-label">Helius API Key</label>
                <div style="display:flex;gap:8px">
                    <input type="text" id="rpc-helius-key" placeholder="your-api-key-here"
                           style="flex:1;font-family:monospace">
                    <button type="button" class="btn btn-sm btn-secondary"
                            onclick="applyHeliusKey()">Apply</button>
                </div>
                <div class="form-helper">
                    Get a free key at <strong>helius.dev</strong> — free tier gives 100k requests/day,
                    much higher rate limits than the public node.
                </div>
            </div>

            <div class="form-group">
                <label class="form-label required">RPC URL (HTTP)</label>
                <input type="url" id="rpc-url" value="${_esc(cfg.rpc_url)}" required
                       placeholder="https://mainnet.helius-rpc.com/?api-key=...">
            </div>

            <div class="form-group">
                <label class="form-label required">WebSocket URL</label>
                <input type="url" id="rpc-ws-url" value="${_esc(cfg.ws_url)}" required
                       placeholder="wss://mainnet.helius-rpc.com/?api-key=...">
                <div class="form-helper">Used for real-time token detection and wallet monitoring.</div>
            </div>

            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Network</label>
                    <select id="rpc-network">
                        <option value="mainnet-beta" ${cfg.network==='mainnet-beta'?'selected':''}>Mainnet Beta</option>
                        <option value="devnet"       ${cfg.network==='devnet'      ?'selected':''}>Devnet</option>
                        <option value="testnet"      ${cfg.network==='testnet'     ?'selected':''}>Testnet</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Commitment</label>
                    <select id="rpc-commitment">
                        <option value="confirmed"  ${cfg.commitment==='confirmed' ?'selected':''}>Confirmed</option>
                        <option value="finalized"  ${cfg.commitment==='finalized' ?'selected':''}>Finalized</option>
                        <option value="processed"  ${cfg.commitment==='processed' ?'selected':''}>Processed</option>
                    </select>
                </div>
            </div>

            <!-- Live connection test -->
            <div id="rpc-test-result" style="margin-bottom:12px"></div>

            <div style="display:flex;gap:8px">
                <button type="button" class="btn btn-secondary" onclick="testRpcConnection()">Test Connection</button>
                <button type="submit" class="btn btn-primary">Save &amp; Reconnect</button>
            </div>
        </form>
    `;
}

/** Fill preset URLs, show/hide Helius key helper */
function applyRpcPreset(preset) {
    const urlEl  = document.getElementById('rpc-url');
    const wsEl   = document.getElementById('rpc-ws-url');
    const netEl  = document.getElementById('rpc-network');
    const helper = document.getElementById('rpc-helius-helper');
    if (!urlEl || !wsEl) return;

    const presets = {
        'helius': {
            rpc: 'https://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY',
            ws:  'wss://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY',
            net: 'mainnet-beta',
        },
        'public-mainnet': {
            rpc: 'https://api.mainnet-beta.solana.com',
            ws:  'wss://api.mainnet-beta.solana.com',
            net: 'mainnet-beta',
        },
        'devnet': {
            rpc: 'https://api.devnet.solana.com',
            ws:  'wss://api.devnet.solana.com',
            net: 'devnet',
        },
    };

    const p = presets[preset];
    if (!p) return;

    urlEl.value = p.rpc;
    wsEl.value  = p.ws;
    if (netEl) netEl.value = p.net;

    if (helper) helper.style.display = (preset === 'helius') ? '' : 'none';

    // Highlight the active button
    document.querySelectorAll('#rpc-settings-body .btn-sm').forEach(b => {
        b.classList.remove('btn-primary');
        b.classList.add('btn-secondary');
    });
    event.target.classList.remove('btn-secondary');
    event.target.classList.add('btn-primary');

    _clearTestResult();
}

/** Replace placeholder API key in the Helius URLs */
function applyHeliusKey() {
    const key   = (document.getElementById('rpc-helius-key')?.value || '').trim();
    const urlEl = document.getElementById('rpc-url');
    const wsEl  = document.getElementById('rpc-ws-url');
    if (!key) { showError('Enter your Helius API key first'); return; }
    if (!urlEl || !wsEl) return;

    urlEl.value = `https://mainnet.helius-rpc.com/?api-key=${key}`;
    wsEl.value  = `wss://mainnet.helius-rpc.com/?api-key=${key}`;
    _clearTestResult();
}

/** Hit /api/health via the currently-typed RPC URL */
async function testRpcConnection() {
    const rpcUrl = document.getElementById('rpc-url')?.value?.trim();
    const result = document.getElementById('rpc-test-result');
    if (!result) return;

    if (!rpcUrl) {
        result.innerHTML = '<span class="text-warning">Enter an RPC URL first.</span>';
        return;
    }

    result.innerHTML = '<span class="text-muted">Testing...</span>';

    try {
        // Ping the Solana RPC directly: getHealth is a lightweight call
        const resp = await fetch(rpcUrl, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'getHealth' }),
            signal:  AbortSignal.timeout(5000),
        });
        const json = await resp.json();
        if (json.result === 'ok' || json.result?.numSlots != null) {
            result.innerHTML = '<span class="text-success">Connected — node is healthy.</span>';
        } else {
            result.innerHTML = `<span class="text-warning">Node responded but returned: ${JSON.stringify(json.result)}</span>`;
        }
    } catch (e) {
        result.innerHTML = `<span class="text-danger">Connection failed: ${e.message}</span>`;
    }
}

async function saveRpcSettings(event) {
    event.preventDefault();

    const data = {
        rpc_url:    document.getElementById('rpc-url').value.trim(),
        ws_url:     document.getElementById('rpc-ws-url').value.trim(),
        network:    document.getElementById('rpc-network').value,
        commitment: document.getElementById('rpc-commitment').value,
    };

    if (data.rpc_url.includes('YOUR_API_KEY') || data.ws_url.includes('YOUR_API_KEY')) {
        showError('Replace YOUR_API_KEY with your actual Helius API key before saving.');
        return;
    }

    try {
        const resp = await api.updateRpcSettings(data);
        if (resp.success) {
            showSuccess('RPC settings saved. Reconnecting...');
            // Refresh to show the new saved values
            setTimeout(() => loadRpcSettings(), 800);
        }
    } catch (e) {
        showError('Failed to save RPC settings: ' + e.message);
    }
}

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

function _esc(str) {
    return (str || '').replace(/"/g, '&quot;');
}

function _clearTestResult() {
    const el = document.getElementById('rpc-test-result');
    if (el) el.innerHTML = '';
}
