/**
 * Wallet Management Component
 */

async function loadWallets() {
    try {
        const response = await api.getWallets();

        if (response.success) {
            renderWalletsTable(response.data);
        }
    } catch (error) {
        console.error('Failed to load wallets:', error);
        showError('Failed to load wallets');
    }
}

function renderWalletsTable(wallets) {
    const tbody = document.querySelector('#wallets-table tbody');

    if (wallets.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No wallets found. Create or import a wallet to get started.</td></tr>';
        return;
    }

    const walletsHTML = wallets.map(wallet => `
        <tr>
            <td><strong>${wallet.name}</strong></td>
            <td class="address-short" onclick="copyToClipboard('${wallet.address}')" title="Click to copy">
                ${shortenAddress(wallet.address)}
            </td>
            <td><span class="badge ${wallet.type}">${wallet.type}</span></td>
            <td>${wallet.balance?.sol?.toFixed(4) || '0.0000'}</td>
            <td>$${wallet.balance?.usd?.toFixed(2) || '0.00'}</td>
            <td>
                ${wallet.is_active ?
                    '<span class="badge active">Active</span>' :
                    '<span class="badge inactive">Inactive</span>'
                }
            </td>
            <td>
                ${!wallet.is_active ?
                    `<button class="btn btn-sm btn-primary" onclick="activateWalletHandler('${wallet.id}')">Activate</button>` :
                    '<button class="btn btn-sm" disabled>Active</button>'
                }
                ${(appState.network || 'devnet') !== 'mainnet-beta'
                    ? `<button class="btn btn-sm btn-success" onclick="requestAirdropHandler('${wallet.id}')">Airdrop</button>`
                    : `<button class="btn btn-sm btn-success" disabled title="Airdrop only available on devnet">Airdrop</button>`
                }
                <button class="btn btn-sm btn-primary" onclick="showTransferModal('${wallet.id}')">Transfer</button>
                <button class="btn btn-sm btn-secondary" onclick="viewWalletDetails('${wallet.id}')">Details</button>
                <button class="btn btn-sm btn-danger" onclick="deleteWalletHandler('${wallet.id}')">Delete</button>
            </td>
        </tr>
    `).join('');

    tbody.innerHTML = walletsHTML;
}

async function activateWalletHandler(walletId) {
    try {
        const response = await api.activateWallet(walletId);

        if (response.success) {
            showSuccess('Wallet activated successfully');
            await loadWallets();
            await loadDashboardData(); // Refresh dashboard
        }
    } catch (error) {
        console.error('Failed to activate wallet:', error);
        showError('Failed to activate wallet');
    }
}

async function deleteWalletHandler(walletId) {
    const confirmed = await showConfirm(
        'Are you sure you want to delete this wallet? This action cannot be undone.',
        'Delete',
        'Cancel'
    );

    if (!confirmed) {
        return;
    }

    try {
        const response = await api.deleteWallet(walletId);

        if (response.success) {
            showSuccess('Wallet deleted successfully');
            await loadWallets();
        }
    } catch (error) {
        console.error('Failed to delete wallet:', error);
        showError('Failed to delete wallet');
    }
}

async function requestAirdropHandler(walletId) {
    showLoading('Requesting airdrop... This may take a few seconds.');

    try {
        const response = await api.requestAirdrop(walletId, 1.0);

        if (response.success) {
            hideLoading();
            showSuccess(`Airdrop of ${response.data.amount} SOL requested successfully! Transaction: ${response.data.signature.substring(0, 8)}...`);
            // Wait a moment for confirmation then reload wallets and dashboard
            setTimeout(async () => {
                await loadWallets();
                if (typeof loadDashboardData === 'function') {
                    await loadDashboardData();
                }
            }, 3000);
        }
    } catch (error) {
        hideLoading();
        console.error('Failed to request airdrop:', error);
        if (error.message.includes('rate') || error.message.includes('limit') || error.message.includes('dry')) {
            showError('Airdrop failed: Rate limited or faucet is dry. Try using faucet.solana.com with GitHub login.');
        } else {
            showError('Failed to request airdrop. Make sure you are on devnet.');
        }
    }
}

async function viewWalletDetails(walletId) {
    try {
        const [walletResponse, balanceResponse] = await Promise.all([
            api.getWallet(walletId),
            api.getWalletBalance(walletId)
        ]);

        if (walletResponse.success && balanceResponse.success) {
            const wallet = walletResponse.data;
            const balance = balanceResponse.data;

            const tokensHTML = balance.tokens?.map(token => `
                <div style="padding: 8px; background: var(--bg-tertiary); border-radius: var(--radius-sm); margin-bottom: 8px;">
                    <strong>${token.symbol}</strong> - ${token.amount} ($${token.usd_value})
                </div>
            `).join('') || '<p class="text-muted">No tokens</p>';

            const content = `
                <div class="form-group">
                    <label>Name:</label>
                    <div>${wallet.name}</div>
                </div>
                <div class="form-group">
                    <label>Address:</label>
                    <div class="address">${wallet.address}</div>
                </div>
                <div class="form-group">
                    <label>Type:</label>
                    <div><span class="badge ${wallet.type}">${wallet.type}</span></div>
                </div>
                <div class="form-group">
                    <label>SOL Balance:</label>
                    <div>${balance.sol} SOL ($${balance.usd})</div>
                </div>
                <div class="form-group">
                    <label>Tokens:</label>
                    <div>${tokensHTML}</div>
                </div>
                <div class="form-group">
                    <label>Last Updated:</label>
                    <div>${formatTimestamp(balance.last_updated)}</div>
                </div>
            `;

            showModal('Wallet Details', content);
        }
    } catch (error) {
        console.error('Failed to load wallet details:', error);
        showError('Failed to load wallet details');
    }
}

function showCreateWalletModal() {
    const content = `
        <form onsubmit="createWalletHandler(event)">
            <div class="form-group">
                <label class="form-label required">Wallet Name</label>
                <input type="text" id="wallet-name" placeholder="Enter wallet name" required>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button type="submit" class="btn btn-primary">Create Wallet</button>
            </div>
        </form>
    `;

    showModal('Create New Wallet', content);
}

async function createWalletHandler(event) {
    event.preventDefault();

    const name = document.getElementById('wallet-name').value;

    closeModal();
    showLoading('Creating wallet...');

    try {
        const response = await api.createWallet(name);

        if (response.success) {
            hideLoading();
            showSuccess('Wallet created successfully');
            await loadWallets();
        }
    } catch (error) {
        hideLoading();
        console.error('Failed to create wallet:', error);
        showError('Failed to create wallet');
    }
}

function showImportWalletModal() {
    const content = `
        <form onsubmit="importWalletHandler(event)">
            <div class="form-group">
                <label class="form-label required">Wallet Name</label>
                <input type="text" id="import-wallet-name" placeholder="Enter wallet name" required>
            </div>
            <div class="form-group">
                <label class="form-label required">Private Key</label>
                <input type="password" id="import-private-key" placeholder="Enter private key" required>
                <div class="form-helper">Your private key will be encrypted and stored locally</div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button type="submit" class="btn btn-primary">Import Wallet</button>
            </div>
        </form>
    `;

    showModal('Import Wallet', content);
}

async function importWalletHandler(event) {
    event.preventDefault();

    const name = document.getElementById('import-wallet-name').value;
    const privateKey = document.getElementById('import-private-key').value;

    closeModal();
    showLoading('Importing wallet...');

    try {
        const response = await api.importWallet({ name, private_key: privateKey });

        if (response.success) {
            hideLoading();
            showSuccess('Wallet imported successfully');
            await loadWallets();
        }
    } catch (error) {
        hideLoading();
        console.error('Failed to import wallet:', error);
        showError('Failed to import wallet');
    }
}

// ---------------------------------------------------------------
// Module wallet assignments
// ---------------------------------------------------------------

const _MODULE_LABELS = {
    sniper:       { label: 'Sniper Bot',    icon: '🎯', desc: 'Wallet used for sniper auto-buy executions.' },
    copy_trading: { label: 'Copy Trading',  icon: '📋', desc: 'Wallet used for copy trading executions.' },
};

async function loadWalletAssignments() {
    try {
        const response = await api.getWalletAssignments();
        if (response.success) {
            renderWalletAssignments(response.data.assignments, response.data.wallets);
        }
    } catch (error) {
        console.error('Failed to load wallet assignments:', error);
        const body = document.getElementById('wallet-assignments-body');
        if (body) body.innerHTML = '<p class="text-danger">Failed to load assignments</p>';
    }
}

function renderWalletAssignments(assignments, wallets) {
    const body = document.getElementById('wallet-assignments-body');
    if (!body) return;

    if (!wallets || wallets.length === 0) {
        body.innerHTML = '<p class="text-muted">No wallets available. Create or import a wallet first.</p>';
        return;
    }

    const walletOptions = wallets.map(w =>
        `<option value="${w.id}">${w.name} (${shortenAddress(w.address)})</option>`
    ).join('');

    const rows = Object.entries(assignments).map(([module, info]) => {
        const meta = _MODULE_LABELS[module] || { label: module, icon: '💼', desc: '' };
        const selectedId = info.wallet_id || '';
        return `
            <div class="wallet-assignment-row">
                <div class="wallet-assignment-info">
                    <span class="wallet-assignment-icon">${meta.icon}</span>
                    <div>
                        <span class="wallet-assignment-label">${meta.label}</span>
                        <span class="wallet-assignment-desc">${meta.desc}</span>
                    </div>
                </div>
                <div class="wallet-assignment-control">
                    <select class="form-control form-control-sm"
                            id="assignment-${module}"
                            onchange="saveWalletAssignment('${module}', this.value)">
                        <option value="">— Use active wallet —</option>
                        ${wallets.map(w =>
                            `<option value="${w.id}" ${w.id === selectedId ? 'selected' : ''}>${w.name} (${shortenAddress(w.address)})</option>`
                        ).join('')}
                    </select>
                    ${info.is_active_fallback
                        ? `<span class="wallet-assignment-badge fallback">Active wallet</span>`
                        : `<span class="wallet-assignment-badge assigned">Assigned</span>`
                    }
                </div>
            </div>
        `;
    }).join('');

    body.innerHTML = `
        <div class="wallet-assignments-grid">${rows}</div>
    `;

    _addAssignmentStyles();
}

async function saveWalletAssignment(module, walletId) {
    try {
        const response = await api.setWalletAssignment(module, walletId || null);
        if (response.success) {
            showSuccess(`${_MODULE_LABELS[module]?.label || module} wallet updated`);
            // Re-render with fresh data to update badges
            renderWalletAssignments(response.data, null);
            // Reload full state to get wallet list back
            await loadWalletAssignments();
        }
    } catch (error) {
        showError('Failed to update wallet assignment');
    }
}

function _addAssignmentStyles() {
    if (document.getElementById('wallet-assignment-styles')) return;
    const style = document.createElement('style');
    style.id = 'wallet-assignment-styles';
    style.textContent = `
        .wallet-assignments-grid {
            display: flex;
            flex-direction: column;
            gap: var(--spacing-sm);
        }
        .wallet-assignment-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: var(--spacing-sm) var(--spacing-md);
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            gap: var(--spacing-md);
            flex-wrap: wrap;
        }
        .wallet-assignment-info {
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            flex: 1;
            min-width: 160px;
        }
        .wallet-assignment-icon {
            font-size: 1.25rem;
        }
        .wallet-assignment-label {
            display: block;
            font-weight: 600;
            font-size: 0.875rem;
        }
        .wallet-assignment-desc {
            display: block;
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        .wallet-assignment-control {
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
        }
        .wallet-assignment-badge {
            font-size: 0.7rem;
            padding: 2px 8px;
            border-radius: var(--radius-sm);
            font-weight: 600;
            white-space: nowrap;
        }
        .wallet-assignment-badge.fallback {
            background: var(--bg-secondary);
            color: var(--text-muted);
        }
        .wallet-assignment-badge.assigned {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-success);
        }
    `;
    document.head.appendChild(style);
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showSuccess('Address copied to clipboard');
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

/**
 * Transfer SOL Modal and Handler
 */
function showTransferModal(walletId) {
    const content = `
        <form onsubmit="transferSOLHandler(event, '${walletId}')">
            <div class="form-group">
                <label class="form-label required">Recipient Address</label>
                <input type="text" id="transfer-to-address" placeholder="Enter recipient Solana address" required>
                <div class="form-helper">The address of the wallet to receive SOL</div>
            </div>
            <div class="form-group">
                <label class="form-label required">Amount (SOL)</label>
                <input type="number" id="transfer-amount" placeholder="0.1" step="0.001" min="0.001" required>
                <div class="form-helper">Amount of SOL to transfer</div>
            </div>
            <div class="form-group">
                <label style="display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" id="transfer-simulate-only">
                    <span>Simulate Only (don't execute)</span>
                </label>
                <div class="form-helper">Check this to simulate the transaction without actually sending it</div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button type="submit" class="btn btn-primary">Transfer SOL</button>
            </div>
        </form>
    `;

    showModal('Transfer SOL', content);
}

async function transferSOLHandler(event, walletId) {
    event.preventDefault();

    const toAddress = document.getElementById('transfer-to-address').value;
    const amount = parseFloat(document.getElementById('transfer-amount').value);
    const simulateOnly = document.getElementById('transfer-simulate-only').checked;

    if (!toAddress || !amount || amount <= 0) {
        showError('Please enter a valid recipient address and amount');
        return;
    }

    closeModal();
    showLoading(simulateOnly ? 'Simulating transfer...' : 'Transferring SOL...');

    try {
        const response = await api.transferSOL(walletId, toAddress, amount, simulateOnly);

        if (response.success) {
            hideLoading();

            if (simulateOnly) {
                showSuccess(`Simulation successful! Would transfer ${amount} SOL to ${toAddress.substring(0, 8)}...`);
            } else {
                showSuccess(`Transfer successful! ${amount} SOL sent to ${toAddress.substring(0, 8)}... Transaction: ${response.data.signature.substring(0, 8)}...`);

                // Wait a moment for confirmation then reload wallets and dashboard
                setTimeout(async () => {
                    await loadWallets();
                    if (typeof loadDashboardData === 'function') {
                        await loadDashboardData();
                    }
                }, 3000);
            }
        }
    } catch (error) {
        hideLoading();
        console.error('Failed to transfer SOL:', error);
        showError(`Transfer failed: ${error.message}`);
    }
}
