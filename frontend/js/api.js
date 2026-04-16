/**
 * API Client
 * Handles all communication with the Flask backend
 */

const API_BASE_URL = 'http://127.0.0.1:5005/api';

class APIClient {
    constructor(baseURL) {
        this.baseURL = baseURL;
    }

    /**
     * Generic request handler
     */
    async request(endpoint, options = {}) {
        const url = `${this.baseURL}${endpoint}`;
        const config = {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers,
            },
            ...options,
        };

        try {
            const response = await fetch(url, config);
            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || `HTTP error! status: ${response.status}`);
            }

            return data;
        } catch (error) {
            console.error('API request failed:', error);
            throw error;
        }
    }

    /**
     * GET request
     */
    async get(endpoint) {
        return this.request(endpoint, { method: 'GET' });
    }

    /**
     * POST request
     */
    async post(endpoint, data) {
        return this.request(endpoint, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /**
     * PUT request
     */
    async put(endpoint, data) {
        return this.request(endpoint, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    /**
     * DELETE request
     */
    async delete(endpoint) {
        return this.request(endpoint, { method: 'DELETE' });
    }

    // === Health & Status ===
    async getHealth() {
        return this.get('/health');
    }

    async getStatus() {
        return this.get('/status');
    }

    // === Wallets ===
    async getWallets() {
        return this.get('/wallets');
    }

    async getWallet(walletId) {
        return this.get(`/wallets/${walletId}`);
    }

    async createWallet(name) {
        return this.post('/wallets', { name });
    }

    async importWallet(data) {
        return this.post('/wallets/import', data);
    }

    async deleteWallet(walletId) {
        return this.delete(`/wallets/${walletId}`);
    }

    async activateWallet(walletId) {
        return this.post(`/wallets/${walletId}/activate`);
    }

    async getWalletBalance(walletId) {
        return this.get(`/wallets/${walletId}/balance`);
    }

    async requestAirdrop(walletId, amount = 1.0) {
        return this.post(`/wallets/${walletId}/airdrop`, { amount });
    }

    async getWalletAssignments() {
        return this.get('/wallets/assignments');
    }

    async setWalletAssignment(module, walletId) {
        return this.put('/wallets/assignments', { module, wallet_id: walletId });
    }

    // === Copy Trading ===
    async startCopyTrading() {
        return this.post('/copy-trading/start');
    }

    async stopCopyTrading() {
        return this.post('/copy-trading/stop');
    }

    async getCTWallets() {
        return this.get('/copy-trading/wallets');
    }

    async addCTWallet(data) {
        return this.post('/copy-trading/wallets', data);
    }

    async updateCTWallet(address, data) {
        return this.put(`/copy-trading/wallets/${address}`, data);
    }

    async removeCTWallet(address) {
        return this.delete(`/copy-trading/wallets/${address}`);
    }

    async toggleCTWallet(address) {
        return this.post(`/copy-trading/wallets/${address}/toggle`);
    }

    async getCTSimPositions(status = null, limit = 200) {
        const params = new URLSearchParams({ limit });
        if (status) params.set('status', status);
        return this.get(`/copy-trading/sim/positions?${params}`);
    }

    async refreshCTSimPrices() {
        return this.post('/copy-trading/sim/refresh');
    }

    async resetCTSimulation() {
        return this.post('/copy-trading/sim/reset');
    }

    async closeCTSimPosition(positionId) {
        return this.post(`/copy-trading/sim/positions/${positionId}/close`);
    }

    async getCopyTradingHistory(limit = 50) {
        return this.get(`/copy-trading/history?limit=${limit}`);
    }

    // === Sniper ===
    async getSniperConfig() {
        return this.get('/sniper/config');
    }

    async updateSniperConfig(data) {
        return this.put('/sniper/config', data);
    }

    async getSniperMLConfig() {
        return this.get('/sniper/config/ml');
    }

    async updateSniperMLConfig(data) {
        return this.put('/sniper/config/ml', data);
    }

    // === AI ===
    async getAIStatus() {
        return this.get('/ai/status');
    }

    async triggerRetrain() {
        return this.post('/ai/retrain');
    }

    async getRetrainStatus() {
        return this.get('/ai/retrain/status');
    }

    async startSniper() {
        return this.post('/sniper/start');
    }

    async stopSniper() {
        return this.post('/sniper/stop');
    }

    async getDetectedTokens(limit = 20) {
        return this.get(`/sniper/detected-tokens?limit=${limit}`);
    }

    async getSniperHistory(limit = 50) {
        return this.get(`/sniper/history?limit=${limit}`);
    }

    // === Anti-Scam ===
    async analyzeToken(tokenAddress) {
        return this.post('/anti-scam/analyze', { token_address: tokenAddress });
    }

    async getAntiScamRules() {
        return this.get('/anti-scam/rules');
    }

    async updateAntiScamRules(data) {
        return this.put('/anti-scam/rules', data);
    }

    async getAntiScamConfig() {
        return this.get('/anti-scam/config');
    }

    async updateAntiScamConfig(data) {
        return this.put('/anti-scam/config', data);
    }

    async getBlacklist() {
        return this.get('/anti-scam/blacklist');
    }

    async addToBlacklist(address, reason, type = 'token') {
        return this.post('/anti-scam/blacklist', { address, reason, type });
    }

    async removeFromBlacklist(address) {
        return this.delete(`/anti-scam/blacklist/${address}`);
    }

    // === AI Analyzer ===
    async aiAnalyzeToken(tokenAddress) {
        return this.post('/ai/analyze-token', { token_address: tokenAddress });
    }

    async aiAnalyzeWallet(walletAddress) {
        return this.post('/ai/analyze-wallet', { wallet_address: walletAddress });
    }

    // === Logs ===
    async getLogs(level = 'all', limit = 100, module = null) {
        let url = `/logs?level=${level}&limit=${limit}`;
        if (module) url += `&module=${module}`;
        return this.get(url);
    }

    async clearLogs() {
        return this.post('/logs/clear');
    }

    // === Metrics ===
    async getMetricsOverview() {
        return this.get('/metrics/overview');
    }

    // === WebSocket ===
    async getWebSocketStatus() {
        return this.get('/websocket/status');
    }

    async startWebSocket() {
        return this.post('/websocket/start');
    }

    async stopWebSocket() {
        return this.post('/websocket/stop');
    }

    async getWebSocketSubscriptions() {
        return this.get('/websocket/subscriptions');
    }

    async subscribeToAccount(accountAddress) {
        return this.post('/websocket/subscribe/account', { account_address: accountAddress });
    }

    async subscribeToProgram(programId) {
        return this.post('/websocket/subscribe/program', { program_id: programId });
    }

    async subscribeToLogs(mentions = null) {
        return this.post('/websocket/subscribe/logs', { mentions });
    }

    async unsubscribeWebSocket(subscriptionId) {
        return this.post(`/websocket/unsubscribe/${subscriptionId}`);
    }

    // === Notifications ===
    async getNotificationStatus() {
        return this.get('/notifications/status');
    }

    async configureNotifications(data) {
        return this.post('/notifications/configure', data);
    }

    async testNotification() {
        return this.post('/notifications/test', {});
    }

    // === Transactions ===
    async transferSOL(fromWalletId, toAddress, amount, simulateOnly = false) {
        return this.post('/transactions/transfer-sol', {
            from_wallet_id: fromWalletId,
            to_address: toAddress,
            amount: amount,
            simulate_only: simulateOnly
        });
    }

    async swapTokens(walletId, inputMint, outputMint, amount, slippageBps = 50, simulateOnly = false) {
        return this.post('/transactions/swap', {
            wallet_id: walletId,
            input_mint: inputMint,
            output_mint: outputMint,
            amount: amount,
            slippage_bps: slippageBps,
            simulate_only: simulateOnly
        });
    }

    async getTransactionHistory(walletId, limit = 10) {
        return this.get(`/transactions/history/${walletId}?limit=${limit}`);
    }

    // ── Settings ──────────────────────────────────────────────────────────

    async getRpcSettings() {
        return this.get('/settings/rpc');
    }

    async updateRpcSettings(data) {
        return this.put('/settings/rpc', data);
    }
}

// Create global API instance
const api = new APIClient(API_BASE_URL);
