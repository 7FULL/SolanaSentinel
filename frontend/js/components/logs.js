/**
 * Logs Viewer Component
 */

async function loadLogs() {
    const level = document.getElementById('log-level-filter').value;

    try {
        const response = await api.getLogs(level, 100);

        if (response.success) {
            renderLogs(response.data);
        }
    } catch (error) {
        console.error('Failed to load logs:', error);
        showError('Failed to load logs');
    }
}

function renderLogs(logs) {
    const container = document.getElementById('logs-container');

    if (logs.length === 0) {
        container.innerHTML = '<p class="text-muted">No logs found</p>';
        return;
    }

    const today = new Date().toLocaleDateString('en-GB', {
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
    });
    const dateLabel = `<div class="logs-date-label">Showing logs for: ${today}</div>`;

    const logsHTML = logs.map(log => `
        <div class="log-entry log-${log.level.toLowerCase()}">
            <div class="log-header">
                <span class="log-level ${getLevelClass(log.level)}">${log.level}</span>
                <span class="log-module">[${log.module}]</span>
                <span class="log-time">${formatTimestamp(log.timestamp)}</span>
            </div>
            <div class="log-message">${log.message}</div>
            ${log.data && Object.keys(log.data).length > 0 ?
                `<div class="log-data"><pre>${JSON.stringify(log.data, null, 2)}</pre></div>` : ''
            }
        </div>
    `).join('');

    container.innerHTML = dateLabel + logsHTML;

    // Add CSS for logs if not already added
    addLogStyles();
}

function getLevelClass(level) {
    const classes = {
        'DEBUG': 'text-muted',
        'INFO': 'text-success',
        'WARNING': 'text-warning',
        'ERROR': 'text-danger',
        'CRITICAL': 'text-danger'
    };
    return classes[level] || '';
}

function addLogStyles() {
    if (document.getElementById('log-styles')) return;

    const style = document.createElement('style');
    style.id = 'log-styles';
    style.textContent = `
        .logs-container {
            font-family: 'Courier New', monospace;
            font-size: 0.875rem;
        }

        .log-entry {
            padding: var(--spacing-md);
            border-left: 3px solid var(--border-color);
            margin-bottom: var(--spacing-sm);
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
        }

        .log-entry.log-error,
        .log-entry.log-critical {
            border-left-color: var(--accent-danger);
            background: rgba(239, 68, 68, 0.05);
        }

        .log-entry.log-warning {
            border-left-color: var(--accent-warning);
            background: rgba(245, 158, 11, 0.05);
        }

        .log-entry.log-info {
            border-left-color: var(--accent-success);
        }

        .log-header {
            display: flex;
            gap: var(--spacing-md);
            margin-bottom: var(--spacing-sm);
            font-size: 0.75rem;
        }

        .log-level {
            font-weight: 600;
            text-transform: uppercase;
        }

        .log-module {
            color: var(--text-secondary);
        }

        .log-time {
            color: var(--text-muted);
            margin-left: auto;
        }

        .log-message {
            color: var(--text-primary);
        }

        .log-data {
            margin-top: var(--spacing-sm);
            padding: var(--spacing-sm);
            background: var(--bg-secondary);
            border-radius: var(--radius-sm);
            overflow-x: auto;
        }

        .log-data pre {
            margin: 0;
            color: var(--text-secondary);
            font-size: 0.75rem;
        }

        .logs-date-label {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: var(--spacing-md);
            padding-bottom: var(--spacing-sm);
            border-bottom: 1px solid var(--border-color);
        }
    `;

    document.head.appendChild(style);
}

async function clearLogs() {
    const confirmed = await showConfirm(
        'Clear today\'s logs? Archived logs are not affected and remain on the server.',
        'Clear All',
        'Cancel'
    );

    if (!confirmed) {
        return;
    }

    try {
        const response = await api.clearLogs();

        if (response.success) {
            showSuccess('Logs cleared successfully');
            await loadLogs();
        }
    } catch (error) {
        console.error('Failed to clear logs:', error);
        showError('Failed to clear logs');
    }
}
