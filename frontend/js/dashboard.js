/**
 * Dashboard Controller
 * Main application logic and navigation
 */

// Application state
const appState = {
    currentSection: 'dashboard',
    activeCTTab: 'wallets',   // tracks which copy-trading sub-tab is open
    apiStatus: 'connecting',
    activeWallet: null,
    systemStatus: null,
};

/**
 * Initialize application
 */
async function initApp() {
    console.log('Initializing SolanaSentinel...');

    // Setup navigation
    setupNavigation();

    // Check API connection
    await checkAPIConnection();

    // Load initial data
    await loadDashboardData();

    // Start auto-refresh
    startAutoRefresh();

    console.log('SolanaSentinel initialized successfully');
}

/**
 * Setup navigation event listeners
 */
function setupNavigation() {
    const navItems = document.querySelectorAll('.nav-item');

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const section = item.dataset.section;
            navigateToSection(section);
        });
    });
}

/**
 * Navigate to a specific section
 */
function navigateToSection(section) {
    // Update nav items
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.section === section) {
            item.classList.add('active');
        }
    });

    // Update content sections
    document.querySelectorAll('.content-section').forEach(sec => {
        sec.classList.remove('active');
    });

    const sectionElement = document.getElementById(`${section}-section`);
    if (sectionElement) {
        sectionElement.classList.add('active');
        appState.currentSection = section;

        // Load section-specific data
        loadSectionData(section);
    }
}

/**
 * Load data for specific section
 */
async function loadSectionData(section) {
    switch (section) {
        case 'dashboard':
            await loadDashboardData();
            break;
        case 'wallets':
            await loadWalletAssignments();
            await loadWallets();
            break;
        case 'copy-trading':
            await loadCopyTradingData();
            break;
        case 'sniper':
            await loadSniperData();
            break;
        case 'anti-scam':
            await loadMLConfig();
            await loadAntiScamConfig();
            await loadBlacklist();
            break;
        case 'logs':
            await loadLogs();
            break;
        case 'notifications':
            await loadNotificationStatus();
            break;
        case 'settings':
            await loadSettingsData();
            break;
    }
}

/**
 * Check API connection
 */
async function checkAPIConnection() {
    try {
        const response = await api.getHealth();

        if (response.success) {
            appState.apiStatus = 'connected';
            updateAPIStatus('connected');
        } else {
            throw new Error('API returned unsuccessful response');
        }
    } catch (error) {
        console.error('API connection failed:', error);
        appState.apiStatus = 'disconnected';
        updateAPIStatus('disconnected');
    }
}

/**
 * Update API status indicator
 */
function updateAPIStatus(status) {
    const statusElement = document.getElementById('api-status');
    const statusDot = statusElement.querySelector('.status-dot');
    const statusText = statusElement.querySelector('span:last-child');

    if (status === 'connected') {
        statusDot.classList.remove('error');
        statusText.textContent = 'Connected';
    } else {
        statusDot.classList.add('error');
        statusText.textContent = 'Disconnected';
    }
}

/**
 * Load dashboard data
 */
async function loadDashboardData() {
    try {
        const [statusResponse, metricsResponse] = await Promise.all([
            api.getStatus(),
            api.getMetricsOverview().catch(() => null)
        ]);

        if (statusResponse.success) {
            appState.systemStatus = statusResponse.data;
            updateDashboardStats(statusResponse.data);
            updateModuleStatus(statusResponse.data.modules);
            updateNetworkBadge(statusResponse.data.network);
        }

        await loadRecentLogs();

    } catch (error) {
        console.error('Failed to load dashboard data:', error);
        showError('Failed to load dashboard data');
    }
}

/**
 * Update dashboard statistics
 */
function updateDashboardStats(data) {
    // Update total wallets
    const totalWallets = data.modules.wallet_manager?.total_wallets || 0;
    document.getElementById('total-wallets').textContent = totalWallets;

    // Update copy trading rules
    const totalRules = data.modules.copy_trading?.total_rules || 0;
    const activeRules = data.modules.copy_trading?.active_rules || 0;
    document.getElementById('copy-rules-count').textContent = totalRules;
    document.getElementById('active-rules-label').textContent = `${activeRules} active`;

    // Update sniper stats
    const tokensDetected = data.modules.sniper?.tokens_detected_today || 0;
    document.getElementById('tokens-detected').textContent = tokensDetected;

    const sniperRunning = data.modules.sniper?.running ? 'Running' : 'Stopped';
    document.getElementById('sniper-status').textContent = sniperRunning;

    const sniperMode = data.modules.sniper?.mode || 'N/A';
    document.getElementById('sniper-mode').textContent = sniperMode;

    // Update active wallet
    let activeWalletText = 'None';
    if (data.active_wallet) {
        if (typeof data.active_wallet === 'object') {
            activeWalletText = data.active_wallet.name || shortenAddress(data.active_wallet.address) || 'Unknown';
        } else {
            activeWalletText = data.active_wallet;
        }
    }
    document.getElementById('active-wallet').textContent = activeWalletText;
}

/**
 * Update the network badge in the header
 */
function updateNetworkBadge(network) {
    const badge = document.getElementById('network-badge');
    if (!badge || !network) return;

    const labels = {
        'devnet':       'Devnet',
        'testnet':      'Testnet',
        'mainnet-beta': 'Mainnet',
    };
    badge.textContent = labels[network] || network;

    // Colour-code so devnet/testnet are visually distinct from mainnet
    badge.style.color = network === 'mainnet-beta' ? '#f7a600' : '#14f195';

    // Store in appState so other components can check
    appState.network = network;
}

/**
 * Update module status grid
 */
function updateModuleStatus(modules) {
    const container = document.getElementById('module-status');

    const modulesHTML = Object.entries(modules).map(([name, data]) => {
        const isActive = data.enabled !== false && data.total_wallets > 0;
        const statusClass = isActive ? 'active' : 'inactive';
        const statusText = isActive ? 'Active' : 'Inactive';

        return `
            <div class="module-item">
                <span class="module-name">${formatModuleName(name)}</span>
                <span class="module-status-badge ${statusClass}">${statusText}</span>
            </div>
        `;
    }).join('');

    container.innerHTML = modulesHTML;
}

/**
 * Format module name for display
 */
function formatModuleName(name) {
    return name.split('_').map(word =>
        word.charAt(0).toUpperCase() + word.slice(1)
    ).join(' ');
}

/**
 * Load recent logs for dashboard
 */
async function loadRecentLogs() {
    try {
        const response = await api.getLogs('all', 10);

        if (response.success && response.data.length > 0) {
            const container = document.getElementById('recent-activity');

            const logsHTML = response.data.map(log => `
                <div class="activity-item">
                    <div class="activity-time">${formatTimestamp(log.timestamp)}</div>
                    <div class="activity-message">
                        <strong>[${log.level}]</strong> [${log.module}] ${log.message}
                    </div>
                </div>
            `).join('');

            container.innerHTML = logsHTML;
        }
    } catch (error) {
        console.error('Failed to load recent logs:', error);
    }
}

/**
 * Start auto-refresh timer
 */
function startAutoRefresh() {
    // Refresh dashboard every 30 seconds
    setInterval(async () => {
        if (appState.currentSection === 'dashboard') {
            await loadDashboardData();
            await updateWebSocketStatus();
        }
    }, 30000);

    // Sniper auto-refresh (live, tracker, simulation tabs) is managed inside sniper.js
    // (_startSniperAutoRefresh) and starts automatically when loadSniperData() is called.

    // Copy-trading auto-refresh is managed inside copy-trading.js (_startCTAutoRefresh)
    // and starts automatically when loadCopyTradingData() is called.

    // Check API connection every 5 seconds
    setInterval(checkAPIConnection, 5000);

    // Update WebSocket status (and event feed) every 5 seconds
    setInterval(updateWebSocketStatus, 5000);
}

/**
 * WebSocket Monitor Functions
 */
async function startWebSocketMonitor() {
    showLoading('Starting WebSocket Monitor...');

    try {
        const response = await api.startWebSocket();

        // Wait a moment for connection to establish
        await new Promise(resolve => setTimeout(resolve, 2000));

        hideLoading();

        if (response.success) {
            document.getElementById('ws-start-btn').style.display = 'none';
            document.getElementById('ws-stop-btn').style.display = 'inline-block';
            await updateWebSocketStatus();
            showSuccess('WebSocket Monitor started successfully');
        }
    } catch (error) {
        hideLoading();
        showError(`Failed to start WebSocket: ${error.message}`);
    }
}

async function stopWebSocketMonitor() {
    try {
        const response = await api.stopWebSocket();
        if (response.success) {
            showSuccess('WebSocket monitor stopped');
            document.getElementById('ws-start-btn').style.display = 'inline-block';
            document.getElementById('ws-stop-btn').style.display = 'none';
            await updateWebSocketStatus();
        }
    } catch (error) {
        showError(`Failed to stop WebSocket: ${error.message}`);
    }
}

async function updateWebSocketStatus() {
    try {
        const response = await api.getWebSocketStatus();
        if (response.success) {
            const data = response.data;

            // Update connection status
            const connected = data.listener?.connected || false;
            document.getElementById('ws-connection-status').textContent = connected ? 'Connected' : 'Disconnected';
            document.getElementById('ws-connection-status').className = `stat-number ${connected ? 'text-success' : 'text-muted'}`;

            // Update subscriptions count
            const subsCount = data.listener?.active_subscriptions || 0;
            document.getElementById('ws-subscriptions-count').textContent = subsCount;

            // Update running status
            const running = data.running;
            document.getElementById('ws-running-status').textContent = running ? 'Yes' : 'No';
            document.getElementById('ws-running-status').className = `stat-number ${running ? 'text-success' : 'text-muted'}`;

            // Update button visibility
            if (running) {
                document.getElementById('ws-start-btn').style.display = 'none';
                document.getElementById('ws-stop-btn').style.display = 'inline-block';
            } else {
                document.getElementById('ws-start-btn').style.display = 'inline-block';
                document.getElementById('ws-stop-btn').style.display = 'none';
            }

            // Refresh subscription list and event feed if running
            if (running) {
                await Promise.all([updateWebSocketEventFeed(), updateWebSocketSubList()]);
            } else {
                renderSubList({});
            }
        }
    } catch (error) {
        console.error('Failed to update WebSocket status:', error);
    }
}

/**
 * Fetch and render the active subscription list.
 */
async function updateWebSocketSubList() {
    try {
        const response = await api.get('/websocket/subscriptions');
        if (response.success) {
            renderSubList(response.data.subscriptions || {});
        }
    } catch (_) {}
}

function renderSubList(subscriptions) {
    const list = document.getElementById('ws-sub-list');
    const counter = document.getElementById('ws-sub-detail-count');
    if (!list) return;

    const entries = Object.entries(subscriptions);
    counter.textContent = entries.length;

    if (entries.length === 0) {
        list.innerHTML = '<span style="color:var(--text-muted);">No active subscriptions</span>';
        return;
    }

    const typeColors = {
        logs:      '#58a6ff',
        account:   '#f0883e',
        program:   '#bc8cff',
        signature: '#3fb950',
    };

    list.innerHTML = entries.map(([id, sub]) => {
        const color = typeColors[sub.type] || '#8b949e';
        const filter = sub.mentions?.length
            ? sub.mentions.map(m => m.slice(0, 256)).join(', ')
            : (sub.address ? sub.address.slice(0, 256): 'all');
        const cb = sub.has_callback ? ' ⚡' : '';
        return `<div style="padding:2px 0; border-bottom:1px solid rgba(255,255,255,0.04);">
            <span style="color:${color}; font-weight:600;">${sub.type}</span>
            <span style="color:var(--text-muted); margin:0 4px;">·</span>
            <span>${filter}${cb}</span>
            <span style="float:right; color:var(--text-muted);">id:${id}</span>
        </div>`;
    }).join('');
}

/**
 * Fetch recent WebSocket events and render the live feed panel.
 */
async function updateWebSocketEventFeed() {
    try {
        const response = await api.get('/websocket/events?limit=20');
        if (!response.success) return;

        const events = response.data.events || [];
        const feed = document.getElementById('ws-event-feed');
        const counter = document.getElementById('ws-event-count');
        if (!feed) return;

        counter.textContent = `${events.length} events`;

        if (events.length === 0) {
            feed.innerHTML = '<span style="color:var(--text-muted);">Waiting for transactions...</span>';
            return;
        }

        feed.innerHTML = events.map(ev => {
            const time = new Date(ev.timestamp).toLocaleTimeString();
            const sig = ev.signature ? ev.signature.slice(0, 12) + '…' : '—';
            const status = ev.err ? '❌' : '✓';
            const preview = ev.log_preview
                ? ev.log_preview.replace(/</g, '&lt;').slice(0, 80)
                : `${ev.log_count} log line(s)`;
            const color = ev.err ? '#f85149' : '#3fb950';
            return `<div style="border-bottom:1px solid rgba(255,255,255,0.06); padding:3px 0;">
                <span style="color:var(--text-muted);">${time}</span>
                <span style="color:${color}; margin:0 4px;">${status}</span>
                <span style="color:#e3b341;">${sig}</span>
                <span style="color:var(--text-secondary); margin-left:6px;">${preview}</span>
            </div>`;
        }).join('');

        // Auto-scroll to top (newest)
        feed.scrollTop = 0;
    } catch (error) {
        // Silently ignore — feed is non-critical
    }
}

/**
 * Utility: Format timestamp
 */
function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString();
}

/**
 * Utility: Format relative time
 */
function formatRelativeTime(timestamp) {
    const now = new Date();
    const then = new Date(timestamp);
    const diff = now - then;

    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);

    if (minutes < 1) return 'Just now';
    if (minutes < 60) return `${minutes}m ago`;
    if (hours < 24) return `${hours}h ago`;
    return `${days}d ago`;
}

/**
 * Utility: Shorten address
 */
function shortenAddress(address, chars = 6) {
    if (!address) return '';
    return `${address.slice(0, chars)}...${address.slice(-chars)}`;
}

/**
 * Utility: Check if WebSocket is running
 * Returns true if running, shows warning and returns false if not
 */
async function checkWebSocketRequired(serviceName) {
    try {
        const response = await api.getWebSocketStatus();
        if (response.success && response.data.running) {
            return true;
        }

        // WebSocket is not running - show warning with option to start
        const shouldStart = await showConfirm(
            `The ${serviceName} requires the WebSocket Monitor to be running for real-time detection.\n\nWould you like to start the WebSocket Monitor now?`,
            'Start WebSocket',
            'Cancel'
        );

        if (shouldStart) {
            showLoading('Starting WebSocket Monitor...');

            try {
                await startWebSocketMonitor();
                // Wait a moment for it to start
                await new Promise(resolve => setTimeout(resolve, 2000));

                // Verify it started
                const checkResponse = await api.getWebSocketStatus();
                hideLoading();

                if (checkResponse.success && checkResponse.data.running) {
                    showSuccess('WebSocket Monitor started successfully. You can now use ' + serviceName);
                    return true;
                } else {
                    showError('Failed to start WebSocket Monitor. Please try starting it manually from the Dashboard.');
                    return false;
                }
            } catch (error) {
                hideLoading();
                showError('Failed to start WebSocket Monitor. Please try starting it manually from the Dashboard.');
                return false;
            }
        }

        return false;
    } catch (error) {
        console.error('Failed to check WebSocket status:', error);
        showError('Unable to verify WebSocket status. Please check the Dashboard.');
        return false;
    }
}

/**
 * Utility: Show error message
 */
function showError(message) {
    const content = `
        <div class="modal-message error">
            <div class="modal-icon">⚠️</div>
            <p>${message}</p>
        </div>
    `;
    const footer = `
        <button class="btn btn-primary" onclick="closeModal()">OK</button>
    `;
    showModal('Error', content, footer);
}

/**
 * Utility: Show success message
 */
function showSuccess(message) {
    const content = `
        <div class="modal-message success">
            <div class="modal-icon">✓</div>
            <p>${message}</p>
        </div>
    `;
    const footer = `
        <button class="btn btn-primary" onclick="closeModal()">OK</button>
    `;
    showModal('Success', content, footer);
}

/**
 * Utility: Show confirmation dialog
 * Returns a promise that resolves to true if confirmed, false if cancelled
 */
function showConfirm(message, confirmText = 'Confirm', cancelText = 'Cancel') {
    return new Promise((resolve) => {
        const content = `
            <div class="modal-message confirm">
                <div class="modal-icon">❓</div>
                <p>${message}</p>
            </div>
        `;
        const footer = `
            <button class="btn btn-secondary" onclick="window.confirmModalResolve(false)">${cancelText}</button>
            <button class="btn btn-primary" onclick="window.confirmModalResolve(true)">${confirmText}</button>
        `;

        // Store resolve function globally so buttons can access it
        window.confirmModalResolve = (result) => {
            closeModal();
            delete window.confirmModalResolve;
            resolve(result);
        };

        showModal('Confirm', content, footer);
    });
}

/**
 * Utility: Show loading modal
 */
function showLoading(message = 'Loading...') {
    const content = `
        <div class="modal-message loading">
            <div class="spinner"></div>
            <p>${message}</p>
        </div>
    `;
    const modalHTML = `
        <div class="modal-overlay modal-no-close">
            <div class="modal" onclick="event.stopPropagation()">
                <div class="modal-body">
                    ${content}
                </div>
            </div>
        </div>
    `;

    const container = document.getElementById('modal-container');
    container.innerHTML = modalHTML;
}

/**
 * Utility: Hide loading modal
 */
function hideLoading() {
    closeModal();
}

/**
 * Utility: Show modal
 */
function showModal(title, content, footer = '') {
    const modalHTML = `
        <div class="modal-overlay" onclick="closeModal(event)">
            <div class="modal" onclick="event.stopPropagation()">
                <div class="modal-header">
                    <h3>${title}</h3>
                    <button class="modal-close" onclick="closeModal()">&times;</button>
                </div>
                <div class="modal-body">
                    ${content}
                </div>
                ${footer ? `<div class="modal-footer">${footer}</div>` : ''}
            </div>
        </div>
    `;

    const container = document.getElementById('modal-container');
    container.innerHTML = modalHTML;
}

/**
 * Utility: Close modal
 */
function closeModal(event) {
    if (!event || event.target.classList.contains('modal-overlay')) {
        document.getElementById('modal-container').innerHTML = '';
    }
}

// Initialize app when DOM is loaded
document.addEventListener('DOMContentLoaded', initApp);
