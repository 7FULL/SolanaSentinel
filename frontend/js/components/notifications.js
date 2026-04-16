/**
 * Notifications Component
 * Manages Telegram and Discord notification configuration.
 */

async function loadNotificationStatus() {
    try {
        const response = await api.getNotificationStatus();

        if (response.success) {
            const data = response.data;

            // Stats
            document.getElementById('notif-enabled-status').textContent =
                data.enabled ? 'Yes' : 'No';
            document.getElementById('notif-sent-count').textContent =
                data.sent_count ?? '-';
            document.getElementById('notif-failed-count').textContent =
                data.failed_count ?? '-';

            // Channel badges
            const tgBadge = document.getElementById('tg-status-badge');
            tgBadge.textContent = data.telegram_configured ? 'Configured' : 'Not configured';
            tgBadge.className = `badge ${data.telegram_configured ? 'active' : 'inactive'}`;

            const discordBadge = document.getElementById('discord-status-badge');
            discordBadge.textContent = data.discord_configured ? 'Configured' : 'Not configured';
            discordBadge.className = `badge ${data.discord_configured ? 'active' : 'inactive'}`;
        }
    } catch (error) {
        console.error('Failed to load notification status:', error);
    }
}

async function saveTelegramConfig(event) {
    event.preventDefault();

    const token  = document.getElementById('tg-token').value.trim();
    const chatId = document.getElementById('tg-chat-id').value.trim();

    if (!token || !chatId) {
        showError('Both Bot Token and Chat ID are required.');
        return;
    }

    try {
        const response = await api.configureNotifications({
            telegram_bot_token: token,
            telegram_chat_id: chatId,
        });

        if (response.success) {
            showSuccess('Telegram configuration saved. Send a test to verify.');
            // Clear sensitive field
            document.getElementById('tg-token').value = '';
            await loadNotificationStatus();
        }
    } catch (error) {
        showError('Failed to save Telegram config: ' + error.message);
    }
}

async function saveDiscordConfig(event) {
    event.preventDefault();

    const webhookUrl = document.getElementById('discord-webhook').value.trim();

    if (!webhookUrl || !webhookUrl.startsWith('https://discord.com/api/webhooks/')) {
        showError('Please enter a valid Discord webhook URL (starts with https://discord.com/api/webhooks/)');
        return;
    }

    try {
        const response = await api.configureNotifications({
            discord_webhook_url: webhookUrl,
        });

        if (response.success) {
            showSuccess('Discord webhook saved. Send a test to verify.');
            document.getElementById('discord-webhook').value = '';
            await loadNotificationStatus();
        }
    } catch (error) {
        showError('Failed to save Discord config: ' + error.message);
    }
}

async function enableNotifications() {
    try {
        await api.configureNotifications({ enabled: true });
        showSuccess('Notifications enabled.');
        await loadNotificationStatus();
    } catch (error) {
        showError('Failed to enable notifications: ' + error.message);
    }
}

async function disableNotifications() {
    const confirmed = await showConfirm(
        'Disable all notifications? You will no longer receive Telegram or Discord alerts.',
        'Disable',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        await api.configureNotifications({ enabled: false });
        showSuccess('Notifications disabled.');
        await loadNotificationStatus();
    } catch (error) {
        showError('Failed to disable notifications: ' + error.message);
    }
}

async function sendTestNotification() {
    showLoading('Sending test notification...');

    try {
        const response = await api.testNotification();
        hideLoading();

        if (response.success) {
            showSuccess(
                'Test notification dispatched! Check your Telegram / Discord within a few seconds.\n\n' +
                'If you did not receive it, verify your Bot Token, Chat ID, and Webhook URL.'
            );
        }
    } catch (error) {
        hideLoading();
        showError('Failed to send test: ' + error.message);
    }
}
