// API Base URL
const API_BASE = '';

// State
let currentView = 'dashboard';
let monitorRunning = false;
let copyTraderRunning = false;
let refreshInterval = null;
let ctTradesRefreshInterval = null;
let ctTerminalRefreshInterval = null;

// Scan history state
let currentScanId = null;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    loadDashboard();
    checkMonitorStatus();
    checkCopyTraderStatus();
    refreshScanHistory();  // Load scan history on startup
    checkApiGuardStatus(); // Check API status on startup

    // Auto-refresh every 30 seconds
    refreshInterval = setInterval(() => {
        if (currentView === 'dashboard') loadDashboard();
        if (currentView === 'positions') loadPositions();
        if (currentView === 'monitor') loadPMConfigs();
        if (currentView === 'copyTrading') loadCopyTrading();
        if (currentView === 'logs') refreshLogs();
        checkMonitorStatus();
        checkCopyTraderStatus();
        checkApiGuardStatus();
    }, 30000);
});

// ============== API Guard Status ==============

async function checkApiGuardStatus() {
    const statusEl = document.getElementById('apiStatus');
    if (!statusEl) return;

    const res = await api('/api/guard/status');
    if (res.success && res.blocked) {
        statusEl.style.display = 'flex';
        statusEl.title = res.error_message || 'API is blocked due to credit issues';
    } else {
        statusEl.style.display = 'none';
    }
}

async function resetApiGuard() {
    const res = await api('/api/guard/reset', { method: 'POST' });
    if (res.success) {
        showToast('API guard reset - you can scan again', 'success');
        checkApiGuardStatus();
    } else {
        showToast('Failed to reset API guard', 'error');
    }
}

// Navigation
function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            showView(view);
        });
    });
}

function showView(viewName) {
    // Update nav
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.view === viewName);
    });

    // Update views
    document.querySelectorAll('.view').forEach(view => {
        view.classList.remove('active');
    });
    document.getElementById(viewName + 'View').classList.add('active');

    currentView = viewName;

    // Stop copy trading trades auto-refresh when leaving that view
    stopCtTradesRefresh();

    // Load data for view
    switch(viewName) {
        case 'dashboard':
            loadDashboard();
            stopLogPolling();
            break;
        case 'opportunities':
            // Don't auto-scan, wait for user
            stopLogPolling();
            break;
        case 'positions':
            loadPositions();
            stopLogPolling();
            break;
        case 'monitor':
            loadPMConfigs();
            stopLogPolling();
            break;
        case 'copyTrading':
            loadCopyTrading();
            startCtTradesRefresh();
            stopLogPolling();
            break;
        case 'logs':
            refreshLogs();
            if (logAutoRefresh) startLogPolling();
            break;
    }
}

// API Helper
async function api(endpoint, options = {}) {
    try {
        // Use AbortController for timeout (5 min for scans/research, 30s for other calls)
        const controller = new AbortController();
        const isLongRunning = endpoint.includes('/scan') || endpoint.includes('/deep-research');
        const timeoutMs = isLongRunning ? 300000 : 30000;
        const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

        const response = await fetch(API_BASE + endpoint, {
            ...options,
            signal: controller.signal,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            }
        });
        clearTimeout(timeoutId);
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        if (error.name === 'AbortError') {
            return { success: false, error: 'Request timed out. The server may still be processing - check the console logs.' };
        }
        return { success: false, error: error.message };
    }
}

// Toast Notifications
function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 4000);
}

// P&L Chart
let pnlChart = null;

// Dashboard
async function loadDashboard() {
    const [positionsRes, pmRes, balanceRes] = await Promise.all([
        api('/api/positions'),
        api('/api/pm/status'),
        api('/api/balance')
    ]);

    let cashAvailable = 0;
    let portfolioValue = 0;

    if (balanceRes.success) {
        cashAvailable = balanceRes.cash_available || 0;
        portfolioValue = balanceRes.invested || 0;  // invested = current value of positions

        document.getElementById('cashAvailable').textContent = '$' + cashAvailable.toFixed(2);
        document.getElementById('portfolioValue').textContent = '$' + portfolioValue.toFixed(2);
        document.getElementById('accountTotal').textContent = '$' + (cashAvailable + portfolioValue).toFixed(2);
    }

    if (positionsRes.success) {
        const positions = positionsRes.positions;
        const totalPnl = positions.reduce((sum, p) => sum + (p.cashPnl || 0), 0);

        document.getElementById('totalPositions').textContent = positions.length;

        const pnlEl = document.getElementById('totalPnl');
        pnlEl.textContent = (totalPnl >= 0 ? '+$' : '-$') + Math.abs(totalPnl).toFixed(2);
        pnlEl.className = 'stat-value ' + (totalPnl >= 0 ? 'positive' : 'negative');
    }

    // Load P&L chart
    loadPnlChart();
}

async function loadPnlChart() {
    const res = await api('/api/pnl-history?hours=168');  // Last 7 days

    const ctx = document.getElementById('pnlChart');
    if (!ctx) return;

    // Prepare chart data
    let labels = [];
    let pnlData = [];

    if (res.success && res.history && res.history.length > 0) {
        res.history.forEach(h => {
            const date = new Date(h.timestamp);
            labels.push(date.toLocaleString([], {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            }));
            pnlData.push(h.pnl);
        });
    } else {
        // No history yet, show placeholder
        labels = ['Now'];
        pnlData = [0];
    }

    const lastPnl = pnlData[pnlData.length - 1] || 0;
    const isPositive = lastPnl >= 0;

    // Destroy existing chart if any
    if (pnlChart) {
        pnlChart.destroy();
    }

    pnlChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Cumulative P&L',
                    data: pnlData,
                    borderColor: isPositive ? '#10b981' : '#ef4444',
                    backgroundColor: isPositive ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 1,
                    pointHoverRadius: 5,
                    borderWidth: 2,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: {
                    display: false,
                },
                tooltip: {
                    backgroundColor: '#1a1a25',
                    titleColor: '#ffffff',
                    bodyColor: '#a0a0b0',
                    borderColor: '#2a2a3a',
                    borderWidth: 1,
                    padding: 12,
                    callbacks: {
                        label: function(context) {
                            const value = context.parsed.y;
                            const sign = value >= 0 ? '+' : '';
                            return `P&L: ${sign}$${value.toFixed(2)}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: {
                        color: '#2a2a3a',
                    },
                    ticks: {
                        color: '#606070',
                        maxTicksLimit: 6,
                        maxRotation: 0,
                    }
                },
                y: {
                    grid: {
                        color: '#2a2a3a',
                    },
                    ticks: {
                        color: '#606070',
                        callback: function(value) {
                            const sign = value >= 0 ? '+' : '';
                            return sign + '$' + value.toFixed(0);
                        }
                    }
                }
            }
        }
    });

    // Update summary below chart if we have data
    if (res.success && res.realized_pnl !== undefined) {
        updatePnlSummary(res.realized_pnl, res.unrealized_pnl, res.total_pnl);
    }
}

function updatePnlSummary(realized, unrealized, total) {
    // This could update a summary section below the chart
    // For now, the Total P&L card already shows this
}

// Monitor Status
async function checkMonitorStatus() {
    const res = await api('/api/pm/status');

    const indicator = document.getElementById('monitorStatusIndicator');
    const btns = [
        document.getElementById('toggleMonitorBtn'),
        document.getElementById('toggleMonitorBtnPositions'),
    ];

    if (res.success) {
        monitorRunning = res.running;

        if (res.running) {
            indicator.className = 'monitor-status running';
            indicator.querySelector('.status-text').textContent = `Monitor: Running (PID: ${res.pid})`;
            btns.forEach(btn => {
                if (btn) { btn.textContent = 'Stop Monitor'; btn.className = 'btn btn-danger'; }
            });
        } else {
            indicator.className = 'monitor-status stopped';
            indicator.querySelector('.status-text').textContent = 'Monitor: Stopped';
            btns.forEach(btn => {
                if (btn) { btn.textContent = 'Start Monitor'; btn.className = 'btn btn-success'; }
            });
        }
    }
}

async function toggleMonitor() {
    const endpoint = monitorRunning ? '/api/pm/stop' : '/api/pm/start';
    const res = await api(endpoint, { method: 'POST' });

    if (res.success) {
        showToast(monitorRunning ? 'Monitor stopped' : 'Monitor started');
        await checkMonitorStatus();
    } else {
        showToast(res.error || 'Operation failed', 'error');
    }
}

// Positions
async function loadPositions() {
    const container = document.getElementById('positionsList');
    container.innerHTML = '<div class="loading">Loading positions...</div>';

    const [posRes, pmRes] = await Promise.all([
        api('/api/positions'),
        api('/api/pm/configs')
    ]);

    if (!posRes.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${posRes.error}</p></div>`;
        return;
    }

    if (!posRes.positions.length) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path>
                </svg>
                <p>No open positions</p>
            </div>
        `;
        return;
    }

    // Build map of token_id -> PM config
    const pmConfigMap = {};
    if (pmRes.success && pmRes.configs) {
        pmRes.configs.forEach(c => {
            pmConfigMap[c.token_id] = c;
        });
    }

    // Calculate totals
    const totalValue = posRes.positions.reduce((sum, p) => sum + (p.currentValue || 0), 0);
    const totalCost = posRes.positions.reduce((sum, p) => sum + (p.avgPrice * p.size || 0), 0);
    const totalPnl = posRes.positions.reduce((sum, p) => sum + (p.cashPnl || 0), 0);
    const totalToWin = posRes.positions.reduce((sum, p) => sum + parseFloat(p.size || 0), 0);

    // Portfolio summary header
    const summaryHtml = `
        <div class="portfolio-summary">
            <div class="portfolio-summary-item">
                <span class="portfolio-summary-label">Total Bet</span>
                <span class="portfolio-summary-value">$${totalCost.toFixed(2)}</span>
            </div>
            <div class="portfolio-summary-item">
                <span class="portfolio-summary-label">To Win</span>
                <span class="portfolio-summary-value">$${totalToWin.toFixed(2)}</span>
            </div>
            <div class="portfolio-summary-item">
                <span class="portfolio-summary-label">Current Value</span>
                <span class="portfolio-summary-value">$${totalValue.toFixed(2)}</span>
            </div>
            <div class="portfolio-summary-item">
                <span class="portfolio-summary-label">Profit/Loss</span>
                <span class="portfolio-summary-value ${totalPnl >= 0 ? 'positive' : 'negative'}">${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}</span>
            </div>
        </div>
    `;

    // Positions table
    const tableHtml = `
        <div class="positions-table-container">
            <table class="positions-table">
                <thead>
                    <tr>
                        <th class="th-market">Market</th>
                        <th class="th-price">Avg → Now</th>
                        <th class="th-bet">Bet</th>
                        <th class="th-towin">To Win</th>
                        <th class="th-value">Value</th>
                        <th class="th-actions">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${posRes.positions.map(p => {
                        const pnlClass = p.cashPnl >= 0 ? 'positive' : 'negative';
                        const pnlSign = p.cashPnl >= 0 ? '+' : '';
                        const pmConfig = pmConfigMap[p.asset];
                        const shares = parseFloat(p.size);
                        const costBasis = p.avgPrice * shares;
                        const toWin = shares; // Each share pays $1 if correct

                        // PM button
                        let pmButton = '';
                        if (pmConfig) {
                            pmButton = `<button class="btn btn-xs btn-outline" onclick="editPMConfig('${pmConfig.id}')" title="TP: $${pmConfig.take_profit_price?.toFixed(2) || '-'} / SL: $${pmConfig.stop_loss_price?.toFixed(2) || '-'}">
                                <span class="pm-active-dot"></span> TP/SL
                            </button>`;
                        } else {
                            pmButton = `<button class="btn btn-xs btn-outline" onclick="addToPM('${p.asset}')">+ TP/SL</button>`;
                        }

                        // Build Polymarket URL
                        const pmUrl = p.eventSlug
                            ? `https://polymarket.com/event/${p.eventSlug}` + (p.slug ? `/${p.slug}` : '')
                            : `https://polymarket.com/markets?_q=${encodeURIComponent(p.title)}`;

                        return `
                            <tr class="position-tr ${pnlClass}">
                                <td class="td-market">
                                    <div class="market-cell">
                                        <span class="outcome-badge ${p.outcome.toLowerCase()}">${p.outcome}</span>
                                        <div class="market-info">
                                            <a href="${pmUrl}" target="_blank" class="market-title market-link">${escapeHtml(p.title)}</a>
                                            <div class="market-shares">${shares.toFixed(2)} shares</div>
                                        </div>
                                    </div>
                                </td>
                                <td class="td-price">
                                    <span class="price-avg">${(p.avgPrice * 100).toFixed(1)}¢</span>
                                    <span class="price-arrow">→</span>
                                    <span class="price-now ${pnlClass}">${(p.curPrice * 100).toFixed(1)}¢</span>
                                </td>
                                <td class="td-bet">
                                    <span class="bet-value">$${costBasis.toFixed(2)}</span>
                                </td>
                                <td class="td-towin">
                                    <span class="towin-value">$${toWin.toFixed(2)}</span>
                                </td>
                                <td class="td-value">
                                    <div class="value-cell">
                                        <span class="value-current">$${p.currentValue.toFixed(2)}</span>
                                        <span class="value-pnl ${pnlClass}">${pnlSign}$${Math.abs(p.cashPnl).toFixed(2)} (${pnlSign}${p.percentPnl.toFixed(1)}%)</span>
                                    </div>
                                </td>
                                <td class="td-actions">
                                    <div class="actions-cell">
                                        <button class="btn btn-xs btn-danger" onclick="sellPosition('${p.asset}', ${p.size})">Sell</button>
                                        ${pmButton}
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        </div>
    `;

    container.innerHTML = summaryHtml + tableHtml;
}

// PM Configs
async function loadPMConfigs() {
    const container = document.getElementById('pmConfigsList');
    container.innerHTML = '<div class="loading">Loading configurations...</div>';

    const res = await api('/api/pm/configs');

    if (!res.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${res.error}</p></div>`;
        return;
    }

    if (!res.configs.length) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
                </svg>
                <p>No configurations</p>
                <button class="btn btn-primary" onclick="showAddAllModal()" style="margin-top: 16px;">
                    Add All Positions
                </button>
            </div>
        `;
        return;
    }

    await checkMonitorStatus();

    container.innerHTML = res.configs.map(c => {
        const pnlClass = c.current_pnl_pct >= 0 ? 'positive' : 'negative';
        const pnlSign = c.current_pnl_pct >= 0 ? '+' : '';
        const statusTag = c.enabled ? '<span class="tag tag-success">ON</span>' : '<span class="tag tag-neutral">OFF</span>';

        // Format TP display: price + gain percentage
        let tpDisplay = '-';
        if (c.take_profit_price) {
            const tpPct = c.take_profit_pct ? `+${c.take_profit_pct.toFixed(1)}%` : '';
            tpDisplay = `$${c.take_profit_price.toFixed(2)} <span class="tag-detail">(${tpPct} gain)</span>`;
        }

        // Format SL display: price + loss percentage
        let slDisplay = '-';
        if (c.stop_loss_price) {
            const slPct = c.stop_loss_pct ? `-${c.stop_loss_pct.toFixed(1)}%` : '';
            slDisplay = `$${c.stop_loss_price.toFixed(2)} <span class="tag-detail">(${slPct} loss)</span>`;
        }

        // Build Polymarket URL
        const pmUrl = c.slug
            ? `https://polymarket.com/event/${c.slug}`
            : `https://polymarket.com/markets?_q=${encodeURIComponent(c.name)}`;

        // Description section
        const descriptionHtml = c.description
            ? `<div class="opp-description">${escapeHtml(c.description.substring(0, 200))}${c.description.length > 200 ? '...' : ''}</div>`
            : '';

        return `
            <div class="card">
                <div class="card-header">
                    <div>
                        <a href="${pmUrl}" target="_blank" class="card-title market-link">${escapeHtml(c.name)}</a>
                        <div class="card-subtitle">${c.side} | ${c.shares.toFixed(2)} shares</div>
                        ${descriptionHtml}
                        <div class="token-id">${c.token_id}</div>
                    </div>
                    <div class="card-actions">
                        ${statusTag}
                        <button class="btn btn-sm btn-outline" onclick="editPMConfig('${c.id}')">Edit</button>
                        <button class="btn btn-sm btn-outline btn-danger" onclick="deletePMConfig('${c.id}')">Delete</button>
                        <button class="btn btn-sm btn-danger" onclick="sellFromPM('${c.token_id}', ${c.shares}, '${c.id}')">Sell</button>
                    </div>
                </div>
                <div class="card-body">
                    <div class="card-stat">
                        <span class="card-stat-label">Entry Price</span>
                        <span class="card-stat-value">$${c.entry_price.toFixed(2)}</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Current Price</span>
                        <span class="card-stat-value ${pnlClass}">${c.current_price ? '$' + c.current_price.toFixed(2) : '-'} ${c.current_pnl_pct !== null ? `<span class="tag-detail">(${pnlSign}${c.current_pnl_pct.toFixed(1)}%)</span>` : ''}</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Take Profit</span>
                        <span class="card-stat-value positive">${tpDisplay}</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Stop Loss</span>
                        <span class="card-stat-value negative">${slDisplay}</span>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function editPMConfig(configId) {
    api(`/api/pm/config/${configId}`).then(res => {
        if (!res.success) {
            showToast(res.error, 'error');
            return;
        }

        const c = res.config;
        const tpPct = c.take_profit_price ? ((c.take_profit_price / c.entry_price) - 1) * 100 : '';
        const slPct = c.stop_loss_price ? (1 - (c.stop_loss_price / c.entry_price)) * 100 : '';

        showModal('Edit Configuration', `
            <div class="form-group">
                <label>Position</label>
                <div style="padding: 8px 0; color: var(--text-primary);">${escapeHtml(c.name)}</div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Take Profit (%)</label>
                    <input type="number" class="input" id="editTpPct" value="${tpPct}" step="0.5" placeholder="e.g. 3">
                </div>
                <div class="form-group">
                    <label>Stop Loss (%)</label>
                    <input type="number" class="input" id="editSlPct" value="${slPct}" step="0.5" placeholder="e.g. 5">
                </div>
            </div>
            <div class="form-group">
                <label>
                    <input type="checkbox" id="editEnabled" ${c.enabled ? 'checked' : ''}> Enabled
                </label>
            </div>
            <div class="form-actions">
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" onclick="savePMConfig('${configId}')">Save</button>
            </div>
        `);
    });
}

async function savePMConfig(configId) {
    const tpPct = parseFloat(document.getElementById('editTpPct').value) / 100 || null;
    const slPct = parseFloat(document.getElementById('editSlPct').value) / 100 || null;
    const enabled = document.getElementById('editEnabled').checked;

    const res = await api(`/api/pm/config/${configId}`, {
        method: 'PUT',
        body: JSON.stringify({
            take_profit_pct: tpPct,
            stop_loss_pct: slPct,
            enabled
        })
    });

    if (res.success) {
        showToast('Configuration updated');
        closeModal();
        if (currentView === 'monitor') loadPMConfigs();
        if (currentView === 'positions') loadPositions();
    } else {
        showToast(res.error, 'error');
    }
}

async function deletePMConfig(configId) {
    if (!confirm('Delete this configuration?')) return;

    const res = await api(`/api/pm/config/${configId}`, { method: 'DELETE' });

    if (res.success) {
        showToast('Configuration deleted');
        if (currentView === 'monitor') loadPMConfigs();
        if (currentView === 'positions') loadPositions();
    } else {
        showToast(res.error, 'error');
    }
}

async function deleteAllConfigs() {
    if (!confirm('Delete ALL configurations? This cannot be undone.')) return;

    const res = await api('/api/pm/delete-all', { method: 'DELETE' });

    if (res.success) {
        showToast(`Deleted ${res.deleted} configuration(s)`);
        loadPMConfigs();
        checkMonitorStatus();
    } else {
        showToast(res.error, 'error');
    }
}

function showAddAllModal() {
    showModal('Add All Positions to PM', `
        <p style="color: var(--text-secondary); margin-bottom: 20px;">
            This will add all your open positions to the Profit Monitor with the specified TP/SL settings.
        </p>
        <div class="form-row">
            <div class="form-group">
                <label>Take Profit (%)</label>
                <input type="number" class="input" id="addAllTpPct" value="3" step="0.5" placeholder="e.g. 3">
            </div>
            <div class="form-group">
                <label>Stop Loss (%)</label>
                <input type="number" class="input" id="addAllSlPct" value="5" step="0.5" placeholder="e.g. 5">
            </div>
        </div>
        <div class="form-group">
            <label>
                <input type="checkbox" id="addAllOverwrite"> Overwrite existing configs
            </label>
        </div>
        <div class="form-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-primary" onclick="submitAddAll()">Add All</button>
        </div>
    `);
}

async function submitAddAll() {
    const tpPct = parseFloat(document.getElementById('addAllTpPct').value) / 100 || null;
    const slPct = parseFloat(document.getElementById('addAllSlPct').value) / 100 || null;
    const overwrite = document.getElementById('addAllOverwrite').checked;

    if (!tpPct && !slPct) {
        showToast('Please specify at least TP or SL', 'error');
        return;
    }

    const res = await api('/api/pm/add-all', {
        method: 'POST',
        body: JSON.stringify({
            take_profit_pct: tpPct,
            stop_loss_pct: slPct,
            overwrite
        })
    });

    if (res.success) {
        showToast(`Added: ${res.added}, Updated: ${res.updated}, Skipped: ${res.skipped}`);
        closeModal();
        loadPMConfigs();
    } else {
        showToast(res.error, 'error');
    }
}

async function addAllToPM() {
    showAddAllModal();
}

function addToPM(tokenId) {
    showModal('Add to Profit Monitor', `
        <div class="form-row">
            <div class="form-group">
                <label>Take Profit (%)</label>
                <input type="number" class="input" id="addTpPct" value="3" step="0.5" placeholder="e.g. 3">
            </div>
            <div class="form-group">
                <label>Stop Loss (%)</label>
                <input type="number" class="input" id="addSlPct" value="5" step="0.5" placeholder="e.g. 5">
            </div>
        </div>
        <div class="form-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-primary" onclick="submitAddToPM('${tokenId}')">Add</button>
        </div>
    `);
}

async function submitAddToPM(tokenId) {
    const tpPct = parseFloat(document.getElementById('addTpPct').value) / 100 || null;
    const slPct = parseFloat(document.getElementById('addSlPct').value) / 100 || null;

    const res = await api('/api/pm/add', {
        method: 'POST',
        body: JSON.stringify({
            token_id: tokenId,
            take_profit_pct: tpPct,
            stop_loss_pct: slPct
        })
    });

    if (res.success) {
        showToast('Added to Profit Monitor');
        closeModal();
        // Refresh positions view to show updated PM status
        if (currentView === 'positions') loadPositions();
    } else {
        showToast(res.error, 'error');
    }
}

// Opportunities
async function scanOpportunities(deepResearch = false) {
    const container = document.getElementById('opportunitiesList');

    const scanType = deepResearch ? 'deep research' : 'standard';
    container.innerHTML = `
        <div class="scan-progress">
            <div class="scan-progress-spinner"></div>
            <div class="scan-progress-text">
                <div class="scan-progress-title">Scanning with ${scanType}...</div>
                <div class="scan-progress-detail">${deepResearch ? 'Performing comprehensive web research (this may take a few minutes)' : 'Fetching markets and analyzing order books'}</div>
            </div>
        </div>
    `;

    const displayCount = parseInt(document.getElementById('topCount').value);
    const risk = document.getElementById('riskMode').value;
    const hours = document.getElementById('maxHours').value;
    const maxAiAnalysis = document.getElementById('maxAiAnalysis').value;

    // Always fetch more opportunities than displayed (up to 100)
    const fetchCount = 100;
    const endpoint = deepResearch ? '/api/scan-deep' : '/api/scan';
    const res = await api(`${endpoint}?top=${fetchCount}&risk=${risk}&hours=${hours}&max_ai=${maxAiAnalysis}`);

    if (!res.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${res.error}</p></div>`;
        return;
    }

    // Build stats summary
    const stats = res.stats || {};
    const riskMode = document.getElementById('riskMode').value;
    const statsHtml = `
        <div class="scan-stats">
            <div class="scan-stats-header">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                </svg>
                <span>Scan Complete</span>
            </div>
            <div class="scan-stats-grid">
                <div class="scan-stat">
                    <span class="scan-stat-value">${stats.markets_fetched || 0}</span>
                    <span class="scan-stat-label">Markets Fetched</span>
                </div>
                <div class="scan-stat">
                    <span class="scan-stat-value">${stats.markets_analyzed || 0}</span>
                    <span class="scan-stat-label">Analyzed</span>
                </div>
                <div class="scan-stat">
                    <span class="scan-stat-value">${res.total_found || 0}</span>
                    <span class="scan-stat-label">Opportunities</span>
                </div>
                <div class="scan-stat">
                    <span class="scan-stat-value">${res.opportunities.length}</span>
                    <span class="scan-stat-label">Showing</span>
                </div>
            </div>
            <div class="scan-filters-summary">
                ${stats.filtered_liquidity ? `<span class="filter-tag" title="Markets with insufficient order book depth">Low liquidity: ${stats.filtered_liquidity}</span>` : ''}
                ${stats.filtered_spread ? `<span class="filter-tag" title="Markets with bid-ask spread too wide">Wide spread: ${stats.filtered_spread}</span>` : ''}
                ${stats.filtered_profit ? `<span class="filter-tag" title="Markets with expected profit below threshold">Low profit: ${stats.filtered_profit}</span>` : ''}
                ${stats.filtered_confidence ? `<span class="filter-tag" title="Markets with confidence score below threshold">Low confidence: ${stats.filtered_confidence}</span>` : ''}
                ${stats.filtered_uncertain ? `<span class="filter-tag" title="Markets with YES price between 40-60%">Too uncertain: ${stats.filtered_uncertain}</span>` : ''}
                ${stats.filtered_claude ? `<span class="filter-tag" title="Markets where Claude recommends SKIP">Claude SKIP: ${stats.filtered_claude}</span>` : ''}
                ${stats.filtered_event ? `<span class="filter-tag" title="Markets where event may have already occurred">Event occurred: ${stats.filtered_event}</span>` : ''}
                ${stats.claude_analyzed ? `<span class="filter-tag info" title="Markets analyzed by Claude AI">Claude analyzed: ${stats.claude_analyzed}</span>` : ''}
                ${stats.claude_skipped ? `<span class="filter-tag warning" title="Markets skipped from AI analysis due to max_ai limit (lower preliminary scores)">AI skipped: ${stats.claude_skipped}</span>` : ''}
                ${stats.facts_skipped ? `<span class="filter-tag warning" title="Markets skipped from facts gathering due to max_ai limit">Facts skipped: ${stats.facts_skipped}</span>` : ''}
            </div>
            ${(stats.triage_passed !== undefined || stats.deep_researched) ? `
            <div class="triage-summary">
                <div class="triage-header">
                    <span class="triage-icon">🎯</span>
                    <span class="triage-title">Deep Research Triage</span>
                </div>
                <div class="triage-stats">
                    ${stats.triage_low_volume ? `<span class="triage-tag filtered" title="Filtered due to low 24h trading volume (<$1000)">Low volume: ${stats.triage_low_volume}</span>` : ''}
                    ${stats.triage_low_confidence ? `<span class="triage-tag filtered" title="Filtered due to Claude confidence below 50%">Low confidence: ${stats.triage_low_confidence}</span>` : ''}
                    ${stats.triage_low_edge ? `<span class="triage-tag filtered" title="Filtered due to edge vs market below 5%">Low edge: ${stats.triage_low_edge}</span>` : ''}
                    ${stats.triage_resolved ? `<span class="triage-tag filtered" title="Filtered because event appears already resolved">Resolved: ${stats.triage_resolved}</span>` : ''}
                    ${stats.triage_passed ? `<span class="triage-tag passed" title="Markets that passed all triage filters">Passed triage: ${stats.triage_passed}</span>` : ''}
                    ${stats.deep_researched ? `<span class="triage-tag researched" title="Markets that received deep research">Deep researched: ${stats.deep_researched}</span>` : ''}
                    ${stats.facts_gathered ? `<span class="triage-tag info" title="Markets with real-time facts gathered">Facts gathered: ${stats.facts_gathered}</span>` : ''}
                </div>
                <div class="triage-cost-savings">
                    ${stats.triage_passed !== undefined && stats.opportunities_found > 0 ?
                        `<span class="savings-text">💰 Saved ${((1 - (stats.triage_passed || 0) / stats.opportunities_found) * 100).toFixed(0)}% on deep research costs</span>` : ''}
                </div>
            </div>
            ` : ''}
            <div class="risk-mode-info">
                Current mode: <strong>${riskMode}</strong>
                <span class="risk-mode-hint">
                    ${riskMode === 'conservative' ? '(Strict filters, fewer opportunities)' :
                      riskMode === 'moderate' ? '(Balanced filters, good for most users)' :
                      riskMode === 'aggressive' ? '(Relaxed filters, more opportunities)' :
                      '(Minimal filters, maximum opportunities)'}
                </span>
            </div>
        </div>
    `;

    if (!res.opportunities.length) {
        container.innerHTML = `
            ${statsHtml}
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                </svg>
                <p>No opportunities found matching criteria</p>
            </div>
        `;
        return;
    }

    // Store all opportunities for "Show All" functionality
    window.allOpportunities = res.opportunities;
    window.displayCount = displayCount;
    window.statsHtml = statsHtml;

    // Initially show only displayCount
    const initialOpps = res.opportunities.slice(0, displayCount);
    const hasMore = res.opportunities.length > displayCount;

    const showAllBtn = hasMore ? `
        <div class="show-all-container">
            <button class="btn btn-outline show-all-btn" onclick="toggleShowAllOpportunities()">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                Show All ${res.opportunities.length} Opportunities (${res.opportunities.length - displayCount} more)
            </button>
        </div>
    ` : '';

    // Use the shared renderOpportunities function to avoid code duplication
    container.innerHTML = statsHtml + renderOpportunities(initialOpps) + showAllBtn;
}

// Enhance a single opportunity with AI analysis
async function enhanceOpportunity(conditionId, tokenId, buttonEl) {
    // Find the opportunity data from stored results
    const allOpps = window.allOpportunities || [];
    const oppIndex = allOpps.findIndex(o => o.condition_id === conditionId || o.token_id === tokenId);

    if (oppIndex === -1) {
        showToast('Opportunity not found', 'error');
        return;
    }

    const opp = allOpps[oppIndex];

    // Update button to show loading state
    const originalHtml = buttonEl.innerHTML;
    buttonEl.disabled = true;
    buttonEl.innerHTML = `
        <svg class="spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="32">
                <animate attributeName="stroke-dashoffset" values="32;0" dur="1s" repeatCount="indefinite"/>
            </circle>
        </svg>
        Enhancing...
    `;

    try {
        const res = await api('/api/enhance-opportunity', {
            method: 'POST',
            body: JSON.stringify({
                condition_id: opp.condition_id,
                token_id: opp.token_id,
                title: opp.title,
                event_title: opp.event_title,
                entry_price: opp.entry_price,
                recommended_side: opp.recommended_side,
                hours_to_expiry: opp.hours_to_expiry
            })
        });

        if (res.success && res.enhanced) {
            // Update the opportunity in storage with AI analysis results
            allOpps[oppIndex].claude_probability = res.claude_probability;
            allOpps[oppIndex].claude_confidence = res.claude_confidence;
            allOpps[oppIndex].claude_recommendation = res.claude_recommendation;
            allOpps[oppIndex].claude_reasoning = res.claude_reasoning;
            allOpps[oppIndex].claude_edge = res.claude_edge;
            allOpps[oppIndex].claude_risk_factors = res.claude_risk_factors || [];
            allOpps[oppIndex].ai_analysis_skipped = false;

            // Re-render the opportunity card in place
            const card = buttonEl.closest('.opportunity-card');
            if (card) {
                const newHtml = renderOpportunities([allOpps[oppIndex]]);
                card.outerHTML = newHtml;
            }

            showToast(`Enhanced: ${res.claude_recommendation}`, 'success');
        } else {
            showToast(res.error || 'Enhancement failed', 'error');
            buttonEl.disabled = false;
            buttonEl.innerHTML = originalHtml;
        }
    } catch (error) {
        showToast('Enhancement error: ' + error.message, 'error');
        buttonEl.disabled = false;
        buttonEl.innerHTML = originalHtml;
    }
}

// Toggle showing all opportunities
function toggleShowAllOpportunities() {
    const container = document.getElementById('opportunitiesList');
    const allOpps = window.allOpportunities || [];
    const displayCount = window.displayCount || 10;
    const statsHtml = window.statsHtml || '';

    // Check current state
    const isShowingAll = container.dataset.showingAll === 'true';

    if (isShowingAll) {
        // Show fewer - back to displayCount
        const limitedOpps = allOpps.slice(0, displayCount);
        const hasMore = allOpps.length > displayCount;

        const showAllBtn = hasMore ? `
            <div class="show-all-container">
                <button class="btn btn-outline show-all-btn" onclick="toggleShowAllOpportunities()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"></polyline>
                    </svg>
                    Show All ${allOpps.length} Opportunities (${allOpps.length - displayCount} more)
                </button>
            </div>
        ` : '';

        container.innerHTML = statsHtml + renderOpportunities(limitedOpps) + showAllBtn;
        container.dataset.showingAll = 'false';
    } else {
        // Show all
        const showLessBtn = `
            <div class="show-all-container">
                <button class="btn btn-outline show-less-btn" onclick="toggleShowAllOpportunities()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="18 15 12 9 6 15"></polyline>
                    </svg>
                    Show Less (Top ${displayCount})
                </button>
            </div>
        `;

        container.innerHTML = statsHtml + renderOpportunities(allOpps) + showLessBtn;
        container.dataset.showingAll = 'true';
    }
}

// ============== Scan History ==============

async function refreshScanHistory() {
    const select = document.getElementById('scanHistory');
    const hint = document.getElementById('historyHint');
    if (!select) return;

    const res = await api('/api/scan/history');
    if (!res.success) return;

    // Keep the current selection
    const currentValue = select.value;

    // Clear and rebuild options
    select.innerHTML = '<option value="">-- Current Results --</option>';

    res.scans.forEach(scan => {
        const option = document.createElement('option');
        option.value = scan.scan_id;

        const typeIcon = scan.scan_type === 'deep' ? '🔬' : '⚡';
        const timeAgo = scan.time_ago;
        const expiresIn = scan.expires_in;
        const count = scan.opportunities_count;

        option.textContent = `${typeIcon} ${timeAgo} - ${count} opps (expires: ${expiresIn})`;
        option.title = `Risk: ${scan.parameters.risk}, Hours: ${scan.parameters.hours}`;

        select.appendChild(option);
    });

    // Restore selection if it still exists
    if (currentValue && select.querySelector(`option[value="${currentValue}"]`)) {
        select.value = currentValue;
    }

    // Update hint
    if (hint) {
        if (res.scans.length === 0) {
            hint.textContent = 'No saved scans';
        } else {
            hint.textContent = `${res.scans.length} saved scan${res.scans.length > 1 ? 's' : ''}`;
        }
    }
}

async function loadHistoricalScan() {
    const select = document.getElementById('scanHistory');
    const scanId = select.value;
    const container = document.getElementById('opportunitiesList');

    if (!scanId) {
        // Clear to show current/empty state
        currentScanId = null;
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                </svg>
                <p>Click "Quick Scan" or "Deep Research Scan" to find opportunities</p>
            </div>
        `;
        return;
    }

    container.innerHTML = '<div class="loading">Loading historical scan...</div>';

    const res = await api(`/api/scan/history/${scanId}`);
    if (!res.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${res.error}</p></div>`;
        return;
    }

    const scan = res.scan;
    currentScanId = scan.scan_id;

    // Build the historical scan header
    const scanDate = new Date(scan.timestamp * 1000).toLocaleString();
    const expiresDate = new Date(scan.expires_at * 1000).toLocaleString();
    const typeLabel = scan.scan_type === 'deep' ? 'Deep Research Scan' : 'Quick Scan';
    const stats = scan.stats || {};

    const headerHtml = `
        <div class="historical-scan-header">
            <div class="historical-scan-info">
                <span class="historical-scan-type ${scan.scan_type}">${typeLabel}</span>
                <span class="historical-scan-date">${scanDate}</span>
                <span class="historical-scan-params">
                    Risk: ${scan.parameters.risk} | Hours: ${scan.parameters.hours} | Max AI: ${scan.parameters.max_ai || 10}
                </span>
            </div>
            <div class="historical-scan-meta">
                <span class="historical-scan-expires">Expires: ${expiresDate}</span>
                <button class="btn btn-outline btn-small btn-danger" onclick="deleteHistoricalScan('${scan.scan_id}')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                        <polyline points="3 6 5 6 21 6"></polyline>
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                    </svg>
                    Delete
                </button>
            </div>
        </div>
        <div class="scan-stats">
            <div class="scan-stats-header">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                <span>Historical Scan</span>
            </div>
            <div class="scan-stats-grid">
                <div class="scan-stat">
                    <span class="scan-stat-value">${stats.markets_fetched || 0}</span>
                    <span class="scan-stat-label">Markets Fetched</span>
                </div>
                <div class="scan-stat">
                    <span class="scan-stat-value">${stats.markets_analyzed || 0}</span>
                    <span class="scan-stat-label">Analyzed</span>
                </div>
                <div class="scan-stat">
                    <span class="scan-stat-value">${scan.opportunities_count}</span>
                    <span class="scan-stat-label">Opportunities</span>
                </div>
                ${stats.deep_researched ? `
                <div class="scan-stat">
                    <span class="scan-stat-value">${stats.deep_researched}</span>
                    <span class="scan-stat-label">Deep Researched</span>
                </div>
                ` : ''}
            </div>
        </div>
    `;

    if (!scan.opportunities.length) {
        container.innerHTML = headerHtml + `
            <div class="empty-state">
                <p>No opportunities were found in this scan</p>
            </div>
        `;
        return;
    }

    // Store for show all functionality
    window.allOpportunities = scan.opportunities;
    window.displayCount = 10;
    window.statsHtml = headerHtml;

    const displayCount = 10;
    const initialOpps = scan.opportunities.slice(0, displayCount);
    const hasMore = scan.opportunities.length > displayCount;

    const showAllBtn = hasMore ? `
        <div class="show-all-container">
            <button class="btn btn-outline show-all-btn" onclick="toggleShowAllOpportunities()">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                Show All ${scan.opportunities.length} Opportunities (${scan.opportunities.length - displayCount} more)
            </button>
        </div>
    ` : '';

    container.innerHTML = headerHtml + renderOpportunities(initialOpps) + showAllBtn;
}

async function deleteHistoricalScan(scanId) {
    if (!confirm('Delete this scan from history?')) return;

    const res = await api(`/api/scan/history/${scanId}`, { method: 'DELETE' });
    if (res.success) {
        showToast('Scan deleted', 'success');
        await refreshScanHistory();

        // If we were viewing this scan, clear the view
        if (currentScanId === scanId) {
            document.getElementById('scanHistory').value = '';
            loadHistoricalScan();
        }
    } else {
        showToast('Failed to delete scan: ' + res.error, 'error');
    }
}

// Render opportunities cards (extracted for reuse)
function renderOpportunities(opportunities) {
    return opportunities.map((opp, index) => {
        const riskClass = opp.risk_score < 0.3 ? 'risk-low' : opp.risk_score < 0.5 ? 'risk-medium' : 'risk-high';
        const riskLabel = opp.risk_score < 0.3 ? 'Low Risk' : opp.risk_score < 0.5 ? 'Medium Risk' : 'High Risk';

        // Claude analysis section
        let claudeSection = '';
        if (opp.claude_recommendation && opp.claude_recommendation !== 'SKIP') {
            const recClass = opp.claude_recommendation === 'BUY_YES' ? 'positive' :
                           opp.claude_recommendation === 'BUY_NO' ? 'positive' : 'neutral';
            const edgeSign = opp.claude_edge >= 0 ? '+' : '';
            const edgeClass = Math.abs(opp.claude_edge) > 0.05 ? 'positive' : 'neutral';

            claudeSection = `
                <div class="claude-analysis">
                    <div class="claude-header">
                        <span class="claude-badge">Claude AI</span>
                        <span class="claude-rec ${recClass}">${opp.claude_recommendation.replace('_', ' ')}</span>
                    </div>
                    <div class="claude-stats">
                        <div class="claude-stat">
                            <span class="claude-stat-label">AI Probability</span>
                            <span class="claude-stat-value">${(opp.claude_probability * 100).toFixed(0)}%</span>
                        </div>
                        <div class="claude-stat">
                            <span class="claude-stat-label">AI Confidence</span>
                            <span class="claude-stat-value">${(opp.claude_confidence * 100).toFixed(0)}%</span>
                        </div>
                        <div class="claude-stat">
                            <span class="claude-stat-label">Edge vs Market</span>
                            <span class="claude-stat-value ${edgeClass}">${edgeSign}${(opp.claude_edge * 100).toFixed(1)}%</span>
                        </div>
                    </div>
                    ${opp.claude_reasoning ? `<div class="claude-reasoning">${escapeHtml(opp.claude_reasoning)}</div>` : ''}
                    ${opp.claude_risk_factors && opp.claude_risk_factors.length > 0 ? `
                        <div class="claude-risks">
                            <span class="claude-risks-label">Risks:</span>
                            ${opp.claude_risk_factors.map(r => `<span class="risk-tag">${escapeHtml(r)}</span>`).join('')}
                        </div>
                    ` : ''}
                </div>
            `;
        }

        // Historical trend section
        let trendSection = '';
        if (opp.price_trend && opp.price_trend !== 'STABLE') {
            const trendIcon = opp.price_trend === 'UP' ? '↑' : '↓';
            const trendClass = opp.price_trend === 'UP' ? 'positive' : 'negative';
            trendSection = `<span class="trend-badge ${trendClass}">${trendIcon} ${opp.price_trend}</span>`;
        }

        // Related markets section
        let relatedSection = '';
        if (opp.related_markets && opp.related_markets.length > 0) {
            relatedSection = `
                <div class="related-markets">
                    <span class="related-label">Related:</span>
                    ${opp.related_markets.slice(0, 2).map(m =>
                        `<span class="related-market">${escapeHtml(m.question.substring(0, 40))}... (${(m.yes_price * 100).toFixed(0)}%)</span>`
                    ).join('')}
                </div>
            `;
        }

        // Event status badge
        let eventBadge = '';
        if (opp.event_status === 'OCCURRED') {
            eventBadge = '<span class="event-badge occurred">Event Occurred</span>';
        } else if (opp.event_status === 'NOT_OCCURRED') {
            eventBadge = '<span class="event-badge not-occurred">Not Occurred</span>';
        }

        // Triage status badge
        let triageBadge = '';
        if (opp.triage_status === 'PASSED') {
            triageBadge = '<span class="triage-badge passed" title="Passed all triage filters - eligible for deep research">✓ Triaged</span>';
        } else if (opp.triage_status === 'FILTERED' && opp.triage_reasons && opp.triage_reasons.length > 0) {
            const reasons = opp.triage_reasons.join(', ');
            triageBadge = `<span class="triage-badge filtered" title="Filtered from deep research: ${escapeHtml(reasons)}">⚠ ${opp.triage_reasons.length} filter${opp.triage_reasons.length > 1 ? 's' : ''}</span>`;
        }

        // AI analysis status badge
        let aiSkippedBadge = '';
        if (opp.ai_analysis_skipped) {
            const scorePercent = ((opp.preliminary_score || 0) * 100).toFixed(0);
            aiSkippedBadge = `<span class="ai-skipped-badge" title="AI analysis skipped due to max_ai limit. Preliminary score: ${scorePercent}%">⏭ No AI (Score: ${scorePercent}%)</span>`;
        }

        // Build Polymarket URL
        const polymarketUrl = opp.polymarket_url || (opp.slug ? `https://polymarket.com/event/${opp.slug}` : '');

        // Title with link
        const titleHtml = polymarketUrl
            ? `<a href="${polymarketUrl}" target="_blank" class="card-title market-link">#${index + 1} ${escapeHtml(opp.title)}</a>`
            : `<div class="card-title">#${index + 1} ${escapeHtml(opp.title)}</div>`;

        // Description section
        const descriptionHtml = opp.description
            ? `<div class="opp-description">${escapeHtml(opp.description.substring(0, 300))}${opp.description.length > 300 ? '...' : ''}</div>`
            : '';

        // Enhance button (only show if no Claude analysis)
        const showEnhanceBtn = !opp.claude_recommendation || opp.claude_recommendation === 'SKIP' || opp.ai_analysis_skipped;
        const enhanceBtnHtml = showEnhanceBtn
            ? `<button class="btn btn-sm btn-outline enhance-btn" onclick="enhanceOpportunity('${opp.condition_id}', '${opp.token_id}', this)" data-opp-index="${index}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
                    <path d="M2 17l10 5 10-5"></path>
                    <path d="M2 12l10 5 10-5"></path>
                </svg>
                Enhance
               </button>`
            : '';

        return `
            <div class="card opportunity-card ${opp.triage_status === 'FILTERED' ? 'triage-filtered' : ''} ${opp.ai_analysis_skipped ? 'ai-skipped' : ''}" data-condition-id="${opp.condition_id}" data-token-id="${opp.token_id}">
                <div class="card-header">
                    <div>
                        ${titleHtml}
                        <div class="card-subtitle">
                            ${escapeHtml(opp.event_title)} | Expires in ${opp.hours_to_expiry.toFixed(1)}h
                            ${trendSection}
                            ${eventBadge}
                            ${triageBadge}
                            ${aiSkippedBadge}
                            ${polymarketUrl ? `<a href="${polymarketUrl}" target="_blank" class="polymarket-link" title="View on Polymarket">🔗 Polymarket</a>` : ''}
                        </div>
                        ${descriptionHtml}
                    </div>
                    <div class="card-actions">
                        <span class="risk-badge ${riskClass}">${riskLabel}</span>
                        ${enhanceBtnHtml}
                        <button class="btn btn-sm btn-success" onclick="executeOpportunity('${opp.token_id}', ${opp.recommended_amount}, '${opp.recommended_side}', ${opp.entry_price})">
                            Buy ${opp.recommended_side}
                        </button>
                    </div>
                </div>
                <div class="card-body">
                    <div class="card-stat">
                        <span class="card-stat-label">Entry Price</span>
                        <span class="card-stat-value">${(opp.entry_price * 100).toFixed(1)}%</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Expected Profit</span>
                        <span class="card-stat-value tag-success">+${(opp.expected_profit_pct * 100).toFixed(1)}%</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Confidence</span>
                        <span class="card-stat-value">${(opp.confidence_score * 100).toFixed(0)}%</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Liquidity</span>
                        <span class="card-stat-value">$${opp.liquidity.toLocaleString()}</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Spread</span>
                        <span class="card-stat-value">${(opp.spread * 100).toFixed(1)}%</span>
                    </div>
                    <div class="card-stat">
                        <span class="card-stat-label">Recommended</span>
                        <span class="card-stat-value">$${opp.recommended_amount.toFixed(2)}</span>
                    </div>
                </div>
                ${claudeSection}
                ${buildResearchFactsSection(opp)}
                ${buildDeepResearchSection(opp)}
                ${relatedSection}
                ${opp.web_context ? `<div class="web-context"><span class="web-context-icon">🔍</span> ${escapeHtml(opp.web_context.substring(0, 200))}${opp.web_context.length > 200 ? '...' : ''}</div>` : ''}
            </div>
        `;
    }).join('');
}

function buildDeepResearchSection(opp) {
    // Check if we have deep research data
    const dr = opp.deep_research || {};
    const hasDeepResearch = opp.deep_research_quality && opp.deep_research_quality !== 'NONE';

    if (!hasDeepResearch && !dr.quality) {
        return '';
    }

    const quality = opp.deep_research_quality || dr.quality || 'NONE';
    const summary = opp.deep_research_summary || dr.summary || '';
    const keyFacts = opp.key_facts || dr.key_facts || [];
    const recentNews = opp.recent_news || dr.recent_news || [];
    const expertOpinions = opp.expert_opinions || dr.expert_opinions || [];
    const contraryEvidence = opp.contrary_evidence || dr.contrary_evidence || [];
    const sentiment = opp.research_sentiment || dr.sentiment || 'NEUTRAL';
    const researchProb = opp.deep_research_probability || dr.probability || 0.5;

    const qualityClass = quality === 'HIGH' ? 'quality-high' :
                        quality === 'MEDIUM' ? 'quality-medium' : 'quality-low';

    const sentimentClass = sentiment === 'POSITIVE' ? 'positive' :
                          sentiment === 'NEGATIVE' ? 'negative' : 'neutral';

    return `
        <div class="deep-research-section">
            <div class="deep-research-header">
                <span class="deep-research-badge">Deep Research</span>
                <span class="quality-badge ${qualityClass}">${quality} Quality</span>
                <span class="sentiment-badge ${sentimentClass}">${sentiment}</span>
                <span class="research-prob">Research Prob: ${(researchProb * 100).toFixed(0)}%</span>
            </div>

            ${summary ? `
                <div class="research-summary">
                    <strong>Summary:</strong> ${escapeHtml(summary)}
                </div>
            ` : ''}

            ${keyFacts.length > 0 ? `
                <div class="research-facts">
                    <div class="research-facts-label">Key Facts:</div>
                    <ul class="research-list">
                        ${keyFacts.slice(0, 3).map(f => `<li>${escapeHtml(f)}</li>`).join('')}
                    </ul>
                </div>
            ` : ''}

            ${recentNews.length > 0 ? `
                <div class="research-news">
                    <div class="research-news-label">Recent News:</div>
                    <ul class="research-list">
                        ${recentNews.slice(0, 2).map(n => `<li>${escapeHtml(n)}</li>`).join('')}
                    </ul>
                </div>
            ` : ''}

            ${expertOpinions.length > 0 ? `
                <div class="research-experts">
                    <div class="research-experts-label">Expert Views:</div>
                    <ul class="research-list">
                        ${expertOpinions.slice(0, 2).map(e => `<li>${escapeHtml(e)}</li>`).join('')}
                    </ul>
                </div>
            ` : ''}

            ${contraryEvidence.length > 0 ? `
                <div class="research-contrary">
                    <div class="research-contrary-label">Contrary Evidence:</div>
                    <ul class="research-list contrary">
                        ${contraryEvidence.slice(0, 2).map(c => `<li>${escapeHtml(c)}</li>`).join('')}
                    </ul>
                </div>
            ` : ''}
        </div>
    `;
}

function buildResearchFactsSection(opp) {
    // Check if we have real-time facts data
    const hasFacts = opp.research_facts && opp.research_facts.length > 0;
    const hasStatus = opp.research_status && opp.research_status.trim().length > 0;
    const hasProgress = opp.research_progress && opp.research_progress.trim().length > 0;

    if (!hasFacts && !hasStatus && !hasProgress) {
        return '';
    }

    const quality = opp.facts_quality || 'UNKNOWN';
    const qualityClass = quality === 'HIGH' ? 'quality-high' :
                        quality === 'MEDIUM' ? 'quality-medium' :
                        quality === 'LOW' ? 'quality-low' : 'quality-unknown';

    // Format the gathered timestamp
    let timeAgo = '';
    if (opp.facts_gathered_at) {
        const gathered = new Date(opp.facts_gathered_at);
        const now = new Date();
        const diffMinutes = Math.floor((now - gathered) / 60000);
        if (diffMinutes < 1) {
            timeAgo = 'just now';
        } else if (diffMinutes < 60) {
            timeAgo = `${diffMinutes}m ago`;
        } else {
            const diffHours = Math.floor(diffMinutes / 60);
            timeAgo = `${diffHours}h ago`;
        }
    }

    return `
        <div class="research-facts-section">
            <div class="research-facts-header">
                <span class="research-facts-badge">Research & Facts</span>
                <span class="quality-badge ${qualityClass}">${quality}</span>
                ${timeAgo ? `<span class="facts-time">Updated ${timeAgo}</span>` : ''}
            </div>

            ${hasProgress ? `
                <div class="research-progress-indicator">
                    <span class="progress-icon">📊</span>
                    <span class="progress-text">${escapeHtml(opp.research_progress)}</span>
                </div>
            ` : ''}

            ${hasStatus ? `
                <div class="research-current-status">
                    <span class="status-label">Current Status:</span>
                    <span class="status-text">${escapeHtml(opp.research_status)}</span>
                </div>
            ` : ''}

            ${hasFacts ? `
                <div class="research-facts-list">
                    <div class="facts-label">Key Data Points:</div>
                    <ul class="facts-items">
                        ${opp.research_facts.slice(0, 5).map(f => `
                            <li class="fact-item">
                                <span class="fact-name">${escapeHtml(f.fact || f)}:</span>
                                <span class="fact-value">${escapeHtml(f.value || '')}</span>
                                ${f.source ? `<span class="fact-source">(${escapeHtml(f.source)})</span>` : ''}
                            </li>
                        `).join('')}
                    </ul>
                </div>
            ` : ''}
        </div>
    `;
}

function executeOpportunity(tokenId, amount, side, entryPrice) {
    showModal('Execute Trade', `
        <div class="form-group">
            <label>Amount ($)</label>
            <input type="number" class="input" id="execAmount" value="${amount.toFixed(2)}" step="1">
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Take Profit (%)</label>
                <input type="number" class="input" id="execTpPct" value="3" step="0.5" placeholder="e.g. 3">
            </div>
            <div class="form-group">
                <label>Stop Loss (%)</label>
                <input type="number" class="input" id="execSlPct" value="5" step="0.5" placeholder="e.g. 5">
            </div>
        </div>
        <div class="form-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-success" onclick="submitExecute('${tokenId}', '${side}')">
                Buy ${side}
            </button>
        </div>
    `);
}

async function submitExecute(tokenId, side) {
    const amount = parseFloat(document.getElementById('execAmount').value);
    const tpPct = parseFloat(document.getElementById('execTpPct').value) / 100 || null;
    const slPct = parseFloat(document.getElementById('execSlPct').value) / 100 || null;

    if (!amount || amount <= 0) {
        showToast('Please enter a valid amount', 'error');
        return;
    }

    const res = await api('/api/execute', {
        method: 'POST',
        body: JSON.stringify({
            token_id: tokenId,
            amount,
            side,
            take_profit_pct: tpPct,
            stop_loss_pct: slPct
        })
    });

    if (res.success) {
        showToast('Order placed successfully!');
        closeModal();
        loadDashboard();
    } else {
        showToast(res.error, 'error');
    }
}

// Sell All
async function sellAllPositions() {
    if (!confirm('Sell ALL positions at market price? This cannot be undone.')) return;

    const res = await api('/api/sell-all', { method: 'POST' });

    if (res.success) {
        showToast(`Sold: ${res.sold}, Failed: ${res.failed}`);
        // Refresh all relevant views
        loadDashboard();
        loadPositions();
        loadPMConfigs();
        checkMonitorStatus();
    } else {
        showToast(res.error, 'error');
    }
}

// Sell Single Position
async function sellPosition(tokenId, size) {
    if (!confirm('Sell this position at market price?')) return;

    const res = await api('/api/sell', {
        method: 'POST',
        body: JSON.stringify({ token_id: tokenId, size: size })
    });

    if (res.success) {
        showToast(res.pm_removed ? 'Position sold and PM config removed' : 'Position sold successfully');
        // Refresh all relevant views
        loadDashboard();
        loadPositions();
        loadPMConfigs();
        checkMonitorStatus();
    } else {
        showToast(res.error || 'Failed to sell position', 'error');
    }
}

// Sell from PM view (API already removes PM config)
async function sellFromPM(tokenId, shares, configId) {
    if (!confirm('Sell this position and remove from Profit Monitor?')) return;

    const res = await api('/api/sell', {
        method: 'POST',
        body: JSON.stringify({ token_id: tokenId, size: shares })
    });

    if (res.success) {
        showToast('Position sold and PM config removed');
        // Refresh all relevant views
        loadDashboard();
        loadPositions();
        loadPMConfigs();
        checkMonitorStatus();
    } else {
        showToast(res.error || 'Failed to sell position', 'error');
    }
}

// Logs
// Log management state
let currentLogChannel = 'profit_monitor';
let logAutoRefresh = true;
let logRefreshInterval = null;
let lastLogTimestamp = {};

function switchLogTab(channel) {
    currentLogChannel = channel;
    lastLogTimestamp[channel] = 0; // Reset to fetch all logs

    // Update tab UI
    document.querySelectorAll('.log-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.channel === channel);
        if (tab.dataset.channel === channel) {
            tab.classList.remove('has-new');
        }
    });

    // Refresh logs for new channel
    refreshLogs();
}

async function refreshLogs() {
    const container = document.getElementById('logsContainer');
    const channel = currentLogChannel;

    // Use different endpoints based on channel
    let res;
    if (channel === 'profit_monitor') {
        // Use the old PM logs endpoint for profit monitor
        res = await api('/api/pm/logs?lines=100');
        if (res.success && res.logs) {
            // Convert to new format
            res.logs = res.logs.map(line => ({
                time: '',
                level: line.includes('Error') || line.includes('failed') ? 'ERROR' :
                       line.includes('SOLD') || line.includes('TRIGGERED') ? 'INFO' : 'INFO',
                message: line
            }));
        }
    } else {
        // Use new logs API for other channels
        res = await api(`/api/logs/${channel}?count=100`);
    }

    if (!res.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${res.error}</p></div>`;
        return;
    }

    const logs = res.logs || [];
    if (!logs.length) {
        container.innerHTML = `<div class="empty-state"><p>No logs available for ${channel}</p></div>`;
        return;
    }

    container.innerHTML = logs.map(entry => {
        const time = entry.time || '';
        const level = entry.level || 'INFO';
        const message = entry.message || '';
        return `
            <div class="log-entry">
                <span class="log-time">${escapeHtml(time)}</span>
                <span class="log-level ${level}">${level}</span>
                <span class="log-message">${escapeHtml(message)}</span>
            </div>
        `;
    }).join('');

    // Scroll to bottom
    container.scrollTop = container.scrollHeight;

    // Update timestamp for incremental updates
    if (res.timestamp) {
        lastLogTimestamp[channel] = res.timestamp;
    }
}

async function pollLogsUpdate() {
    if (!logAutoRefresh) return;

    const channel = currentLogChannel;

    // Use different endpoints based on channel
    let res;
    if (channel === 'profit_monitor') {
        res = await api('/api/pm/logs?lines=100');
        if (res.success && res.logs) {
            res.logs = res.logs.map(line => ({
                time: '',
                level: line.includes('Error') || line.includes('failed') ? 'ERROR' : 'INFO',
                message: line
            }));
        }
    } else {
        const since = lastLogTimestamp[channel] || 0;
        res = await api(`/api/logs/${channel}?since=${since}`);
    }

    if (res.success && res.logs && res.logs.length > 0) {
        const container = document.getElementById('logsContainer');
        const wasAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 50;

        // Append new entries
        res.logs.forEach(entry => {
            const div = document.createElement('div');
            div.className = 'log-entry';
            div.innerHTML = `
                <span class="log-time">${escapeHtml(entry.time || '')}</span>
                <span class="log-level ${entry.level || 'INFO'}">${entry.level || 'INFO'}</span>
                <span class="log-message">${escapeHtml(entry.message || '')}</span>
            `;
            container.appendChild(div);
        });

        // Auto-scroll if was at bottom
        if (wasAtBottom) {
            container.scrollTop = container.scrollHeight;
        }

        // Update timestamp
        if (res.timestamp) {
            lastLogTimestamp[channel] = res.timestamp;
        }

        // Flash indicator on other tabs if they have new logs
        if (channel !== currentLogChannel) {
            const tab = document.querySelector(`.log-tab[data-channel="${channel}"]`);
            if (tab) tab.classList.add('has-new');
        }
    }
}

function toggleAutoRefreshLogs() {
    logAutoRefresh = document.getElementById('autoRefreshLogs').checked;
    if (logAutoRefresh) {
        startLogPolling();
    } else {
        stopLogPolling();
    }
}

function startLogPolling() {
    if (logRefreshInterval) clearInterval(logRefreshInterval);
    logRefreshInterval = setInterval(pollLogsUpdate, 2000);
}

function stopLogPolling() {
    if (logRefreshInterval) {
        clearInterval(logRefreshInterval);
        logRefreshInterval = null;
    }
}

async function clearCurrentLogs() {
    const channel = currentLogChannel;
    if (channel !== 'profit_monitor') {
        await api(`/api/logs/${channel}/clear`, { method: 'POST' });
    }
    refreshLogs();
}

// Modal
function showModal(title, content) {
    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalBody').innerHTML = content;
    document.getElementById('modalOverlay').classList.add('active');
}

function closeModal() {
    document.getElementById('modalOverlay').classList.remove('active');
}

// Search state
let currentSearchPage = 1;
let currentSearchQuery = '';

// Search Markets
async function searchMarkets(page = 1) {
    const query = document.getElementById('marketSearchInput').value.trim();
    const container = document.getElementById('searchResults');

    if (!query) {
        showToast('Please enter a search term', 'error');
        return;
    }

    currentSearchQuery = query;
    currentSearchPage = page;

    container.innerHTML = `
        <div class="scan-progress">
            <div class="scan-progress-spinner"></div>
            <div class="scan-progress-text">
                <div class="scan-progress-title">Searching markets...</div>
                <div class="scan-progress-detail">Looking for "${escapeHtml(query)}"${page > 1 ? ` (page ${page})` : ''}</div>
            </div>
        </div>
    `;

    const activeOnly = document.getElementById('searchActiveOnly').checked;
    const limit = document.getElementById('searchLimit').value;

    const res = await api(`/api/search?q=${encodeURIComponent(query)}&active=${activeOnly}&limit=${limit}&page=${page}`);

    if (!res.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${res.error}</p></div>`;
        return;
    }

    if (!res.markets || !res.markets.length) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                </svg>
                <p>No markets found for "${escapeHtml(query)}"</p>
            </div>
        `;
        return;
    }

    // Pagination info
    const pageInfo = res.total_pages > 1
        ? ` (page ${res.page} of ${res.total_pages})`
        : '';
    const resultCount = `<div class="search-result-count">Found <strong>${res.total_results}</strong> market${res.total_results !== 1 ? 's' : ''} for "<strong>${escapeHtml(res.query)}</strong>"${pageInfo}</div>`;

    container.innerHTML = resultCount + res.markets.map(m => {
        const yesPrice = m.yes_price ? (m.yes_price * 100).toFixed(0) : '-';
        const noPrice = m.no_price ? (m.no_price * 100).toFixed(0) : '-';

        // Format volume
        let volumeStr = '-';
        if (m.volume) {
            if (m.volume >= 1000000) {
                volumeStr = `$${(m.volume / 1000000).toFixed(1)}M`;
            } else if (m.volume >= 1000) {
                volumeStr = `$${(m.volume / 1000).toFixed(1)}K`;
            } else {
                volumeStr = `$${m.volume.toFixed(0)}`;
            }
        }

        // Format liquidity
        let liquidityStr = '-';
        if (m.liquidity) {
            if (m.liquidity >= 1000000) {
                liquidityStr = `$${(m.liquidity / 1000000).toFixed(1)}M`;
            } else if (m.liquidity >= 1000) {
                liquidityStr = `$${(m.liquidity / 1000).toFixed(1)}K`;
            } else {
                liquidityStr = `$${m.liquidity.toFixed(0)}`;
            }
        }

        const statusClass = m.closed ? 'tag-neutral' : 'tag-success';
        const statusText = m.closed ? 'Closed' : 'Active';

        // Format end date
        let endDateStr = '';
        if (m.end_date) {
            const endDate = new Date(m.end_date);
            const now = new Date();
            const hoursToEnd = (endDate - now) / (1000 * 60 * 60);
            if (hoursToEnd <= 0) {
                endDateStr = 'Ended';
            } else if (hoursToEnd < 1) {
                endDateStr = `${Math.round(hoursToEnd * 60)}m`;
            } else if (hoursToEnd < 24) {
                endDateStr = `${hoursToEnd.toFixed(1)}h`;
            } else if (hoursToEnd < 168) {
                endDateStr = `${Math.round(hoursToEnd / 24)}d`;
            } else {
                endDateStr = `${Math.round(hoursToEnd / 168)}w`;
            }
        }

        // Build token rows for trading
        let tokenRows = '';
        if (m.tokens && m.tokens.length > 0) {
            tokenRows = m.tokens.map(t => {
                const priceDisplay = t.price ? `${(t.price * 100).toFixed(0)}¢` : '-';
                return `
                <div class="token-row">
                    <div class="token-outcome">
                        <span class="outcome-badge ${t.outcome.toLowerCase()}">${t.outcome}</span>
                        <span style="color: var(--text-muted); font-size: 12px;">${priceDisplay}</span>
                        <span class="token-id">${t.token_id}</span>
                    </div>
                    <div class="token-actions">
                        <button class="btn btn-sm btn-success" onclick="showBuyModal('${t.token_id}', \`${escapeHtml(m.question).replace(/`/g, '\\`')}\`, '${t.outcome}', ${t.price || 0.5})">
                            Buy ${t.outcome}
                        </button>
                    </div>
                </div>
            `}).join('');
        }

        // Build Polymarket URL
        const pmUrl = m.slug
            ? `https://polymarket.com/event/${m.slug}`
            : `https://polymarket.com/markets?_q=${encodeURIComponent(m.question)}`;

        return `
            <div class="market-card">
                <div class="market-card-header">
                    <div class="market-card-info">
                        <a href="${pmUrl}" target="_blank" class="market-card-title market-link">${escapeHtml(m.question)}</a>
                        ${m.event_title && m.event_title !== m.question ? `<div class="market-card-event">${escapeHtml(m.event_title)}</div>` : ''}
                        <div class="market-card-meta">
                            <span class="tag ${statusClass}">${statusText}</span>
                            ${endDateStr ? `<span class="market-meta-item">Ends in ${endDateStr}</span>` : ''}
                            <span class="market-meta-item">Vol ${volumeStr}</span>
                            <span class="market-meta-item">Liq ${liquidityStr}</span>
                        </div>
                    </div>
                    <div class="market-card-prices">
                        <div class="price-box yes">
                            <div class="price-box-label">Yes</div>
                            <div class="price-box-value">${yesPrice}¢</div>
                        </div>
                        <div class="price-box no">
                            <div class="price-box-label">No</div>
                            <div class="price-box-value">${noPrice}¢</div>
                        </div>
                    </div>
                </div>
                ${tokenRows ? `<div class="market-card-tokens">${tokenRows}</div>` : ''}
            </div>
        `;
    }).join('');

    // Add pagination controls
    let paginationHtml = '';
    if (res.total_pages > 1) {
        const prevDisabled = res.page <= 1 ? 'disabled' : '';
        const nextDisabled = res.page >= res.total_pages ? 'disabled' : '';

        paginationHtml = `
            <div class="pagination">
                <button class="btn btn-outline btn-sm" onclick="searchMarkets(${res.page - 1})" ${prevDisabled}>
                    ← Previous
                </button>
                <span class="pagination-info">Page ${res.page} of ${res.total_pages}</span>
                <button class="btn btn-outline btn-sm" onclick="searchMarkets(${res.page + 1})" ${nextDisabled}>
                    Next →
                </button>
            </div>
        `;
    }

    container.innerHTML += paginationHtml;
}

// Buy Modal for search results
function showBuyModal(tokenId, title, outcome, currentPrice) {
    showModal(`Buy ${outcome}`, `
        <div class="form-group">
            <label>Market</label>
            <div style="padding: 8px 0; color: var(--text-primary); font-size: 14px;">${title}</div>
        </div>
        <div class="form-group">
            <label>Amount ($)</label>
            <input type="number" class="input" id="buyAmount" value="10" step="1" min="1">
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Take Profit (%)</label>
                <input type="number" class="input" id="buyTpPct" value="3" step="0.5" placeholder="e.g. 3">
            </div>
            <div class="form-group">
                <label>Stop Loss (%)</label>
                <input type="number" class="input" id="buySlPct" value="5" step="0.5" placeholder="e.g. 5">
            </div>
        </div>
        <div style="padding: 12px; background: var(--bg-tertiary); border-radius: var(--radius-md); margin-bottom: 16px;">
            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 4px;">Current Price</div>
            <div style="font-size: 18px; font-weight: 600; font-family: var(--font-mono);">${(currentPrice * 100).toFixed(1)}¢</div>
        </div>
        <div class="form-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-success" onclick="submitBuy('${tokenId}', '${outcome}')">
                Buy ${outcome}
            </button>
        </div>
    `);
}

async function submitBuy(tokenId, side) {
    const amount = parseFloat(document.getElementById('buyAmount').value);
    const tpPct = parseFloat(document.getElementById('buyTpPct').value) / 100 || null;
    const slPct = parseFloat(document.getElementById('buySlPct').value) / 100 || null;

    if (!amount || amount <= 0) {
        showToast('Please enter a valid amount', 'error');
        return;
    }

    const res = await api('/api/execute', {
        method: 'POST',
        body: JSON.stringify({
            token_id: tokenId,
            amount,
            side,
            take_profit_pct: tpPct,
            stop_loss_pct: slPct
        })
    });

    if (res.success) {
        showToast('Order placed successfully!');
        closeModal();
        loadDashboard();
    } else {
        showToast(res.error, 'error');
    }
}

// Deep Research on Demand
async function deepResearchMarket(conditionId, title, yesPrice) {
    showModal('Deep Research', `
        <div class="deep-research-loading">
            <div class="scan-progress-spinner"></div>
            <div style="margin-top: 16px;">
                <strong>Researching:</strong> ${escapeHtml(title.substring(0, 100))}...
            </div>
            <div style="margin-top: 8px; color: var(--text-secondary); font-size: 13px;">
                Searching the web, analyzing news, gathering expert opinions...
            </div>
        </div>
    `);

    const res = await api('/api/deep-research', {
        method: 'POST',
        body: JSON.stringify({
            condition_id: conditionId,
            title: title,
            yes_price: yesPrice || 0.5
        })
    });

    if (!res.success) {
        document.getElementById('modalBody').innerHTML = `
            <div class="empty-state">
                <p>Research failed: ${escapeHtml(res.error)}</p>
                <button class="btn btn-secondary" onclick="closeModal()">Close</button>
            </div>
        `;
        return;
    }

    const research = res.research || {};
    const analysis = res.analysis || {};

    const qualityClass = research.research_quality === 'HIGH' ? 'quality-high' :
                        research.research_quality === 'MEDIUM' ? 'quality-medium' : 'quality-low';

    const probDisplay = (res.final_probability * 100).toFixed(0);
    const confDisplay = (res.final_confidence * 100).toFixed(0);
    const edgeDisplay = (res.edge * 100).toFixed(1);
    const edgeClass = Math.abs(res.edge) > 0.05 ? (res.edge > 0 ? 'positive' : 'negative') : 'neutral';

    document.getElementById('modalBody').innerHTML = `
        <div class="deep-research-result">
            <div class="research-result-header">
                <span class="quality-badge ${qualityClass}">${research.research_quality || 'N/A'} Quality</span>
                <span class="sentiment-badge">${research.sentiment || 'NEUTRAL'}</span>
                <span class="sources-count">${research.sources_found || 0} sources</span>
            </div>

            <div class="research-result-recommendation">
                <div class="rec-box">
                    <div class="rec-label">Final Probability</div>
                    <div class="rec-value">${probDisplay}%</div>
                </div>
                <div class="rec-box">
                    <div class="rec-label">Confidence</div>
                    <div class="rec-value">${confDisplay}%</div>
                </div>
                <div class="rec-box">
                    <div class="rec-label">Edge</div>
                    <div class="rec-value ${edgeClass}">${res.edge >= 0 ? '+' : ''}${edgeDisplay}%</div>
                </div>
                <div class="rec-box rec-main">
                    <div class="rec-label">Recommendation</div>
                    <div class="rec-value ${res.recommendation === 'SKIP' ? 'neutral' : 'positive'}">${res.recommendation || 'SKIP'}</div>
                </div>
            </div>

            ${research.executive_summary ? `
                <div class="research-result-summary">
                    <strong>Executive Summary:</strong><br>
                    ${escapeHtml(research.executive_summary)}
                </div>
            ` : ''}

            ${res.reasoning ? `
                <div class="research-result-reasoning">
                    <strong>Reasoning:</strong><br>
                    ${escapeHtml(res.reasoning)}
                </div>
            ` : ''}

            ${research.key_facts && research.key_facts.length > 0 ? `
                <div class="research-result-section">
                    <strong>Key Facts:</strong>
                    <ul>${research.key_facts.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul>
                </div>
            ` : ''}

            ${research.recent_news && research.recent_news.length > 0 ? `
                <div class="research-result-section">
                    <strong>Recent News:</strong>
                    <ul>${research.recent_news.map(n => `<li>${escapeHtml(n)}</li>`).join('')}</ul>
                </div>
            ` : ''}

            ${research.expert_opinions && research.expert_opinions.length > 0 ? `
                <div class="research-result-section">
                    <strong>Expert Opinions:</strong>
                    <ul>${research.expert_opinions.map(e => `<li>${escapeHtml(e)}</li>`).join('')}</ul>
                </div>
            ` : ''}

            ${research.contrary_evidence && research.contrary_evidence.length > 0 ? `
                <div class="research-result-section contrary">
                    <strong>Contrary Evidence:</strong>
                    <ul>${research.contrary_evidence.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul>
                </div>
            ` : ''}

            <div class="form-actions" style="margin-top: 24px;">
                <button class="btn btn-secondary" onclick="closeModal()">Close</button>
            </div>
        </div>
    `;
}

// ============== Copy Trading ==============

async function checkCopyTraderStatus() {
    const res = await api('/api/ct/status');

    const indicator = document.getElementById('copyTraderStatusIndicator');
    const btn = document.getElementById('toggleCopyTraderBtn');

    if (res.success) {
        copyTraderRunning = res.running;

        if (indicator) {
            if (res.running) {
                indicator.className = 'monitor-status running';
                indicator.querySelector('.status-text').textContent = `Copy Trader: Running (PID: ${res.pid})`;
            } else {
                indicator.className = 'monitor-status stopped';
                indicator.querySelector('.status-text').textContent = 'Copy Trader: Stopped';
            }
        }

        if (btn) {
            if (res.running) {
                btn.textContent = 'Stop Copy Trader';
                btn.className = 'btn btn-danger';
            } else {
                btn.textContent = 'Start Copy Trader';
                btn.className = 'btn btn-success';
            }
        }
    }
}

async function toggleCopyTrader() {
    const endpoint = copyTraderRunning ? '/api/ct/stop' : '/api/ct/start';
    const res = await api(endpoint, { method: 'POST' });

    if (res.success) {
        showToast(copyTraderRunning ? 'Copy trader stopped' : 'Copy trader started');
        await checkCopyTraderStatus();
    } else {
        showToast(res.error || 'Operation failed', 'error');
    }
}

function renderCtTradesTable(trades, containerId) {
    const container = document.getElementById(containerId);
    if (!trades || !trades.length) {
        container.innerHTML = '<div class="empty-state"><p>No trades yet</p></div>';
        return;
    }

    const rows = trades.map(t => {
        const sideClass = t.side === 'BUY' ? 'ct-side-buy' : 'ct-side-sell';
        const entryPrice = t.price ? (t.price * 100).toFixed(1) + '\u00A2' : '-';
        const curPrice = t.current_price ? (t.current_price * 100).toFixed(1) + '\u00A2' : '-';
        const priceClass = t.current_price && t.price
            ? (t.current_price >= t.price ? 'ct-price-up' : 'ct-price-down')
            : '';
        const tradeSize = t.usdc_size != null ? '$' + parseFloat(t.usdc_size).toFixed(2) : '-';
        const toWin = t.size != null ? parseFloat(t.size).toFixed(2) : '-';
        const value = t.current_value != null ? '$' + parseFloat(t.current_value).toFixed(2) : '-';
        const dt = t.timestamp ? new Date(t.timestamp * 1000) : null;
        const dateStr = dt ? dt.toLocaleDateString(undefined, {month:'short', day:'numeric'}) : '-';
        const timeStr = dt ? dt.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'}) : '';
        const handle = t.handle ? '@' + escapeHtml(t.handle) : '';
        const title = escapeHtml(t.title || 'Unknown');
        const outcome = t.outcome || '';

        return `<tr>
            <td class="${sideClass}">${t.side}</td>
            <td class="ct-market-cell" title="${title}">
                <div>${outcome ? '<span class="outcome-badge ' + outcome.toLowerCase() + '" style="font-size:9px;padding:2px 6px;margin-right:4px;">' + outcome + '</span>' : ''}${title}</div>
                ${handle ? '<div class="ct-handle">' + handle + '</div>' : ''}
            </td>
            <td class="td-center">${entryPrice} <span class="price-arrow">\u2192</span> <span class="${priceClass}">${curPrice}</span></td>
            <td class="td-right">${tradeSize}</td>
            <td class="td-right">${toWin}</td>
            <td class="td-right">${value}</td>
            <td class="ct-date">${dateStr}<br>${timeStr}</td>
        </tr>`;
    }).join('');

    container.innerHTML = `
        <table class="ct-trades-table">
            <thead>
                <tr>
                    <th>Activity</th>
                    <th>Market</th>
                    <th class="th-center">Avg \u2192 Now</th>
                    <th class="th-right">Size</th>
                    <th class="th-right">To Win</th>
                    <th class="th-right">Value</th>
                    <th>Date/Time</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

function formatRunTimestamp(ts) {
    if (!ts) return '';
    const dt = new Date(ts * 1000);
    return dt.toLocaleString(undefined, {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
}

async function refreshCtTrades() {
    const [detectedRes, executedRes] = await Promise.all([
        api('/api/ct/detected-trades'),
        api('/api/ct/executed-trades'),
    ]);

    renderCtTradesTable(detectedRes.success ? detectedRes.trades : [], 'ctDetectedTrades');
    renderCtTradesTable(executedRes.success ? executedRes.trades : [], 'ctExecutedTrades');

    // Update timestamps
    const detectedTs = document.getElementById('ctDetectedTimestamp');
    const executedTs = document.getElementById('ctExecutedTimestamp');
    if (detectedTs) detectedTs.textContent = detectedRes.run_timestamp ? formatRunTimestamp(detectedRes.run_timestamp) : '';
    if (executedTs) executedTs.textContent = executedRes.run_timestamp ? formatRunTimestamp(executedRes.run_timestamp) : '';
}

function startCtTradesRefresh() {
    stopCtTradesRefresh();
    // Refresh trades tables every 10s, terminal logs every 5s
    ctTradesRefreshInterval = setInterval(() => {
        if (currentView === 'copyTrading') {
            refreshCtTrades();
        }
    }, 10000);
    ctTerminalRefreshInterval = setInterval(() => {
        if (currentView === 'copyTrading') {
            refreshCtTerminal();
        }
    }, 5000);
}

function stopCtTradesRefresh() {
    if (ctTradesRefreshInterval) {
        clearInterval(ctTradesRefreshInterval);
        ctTradesRefreshInterval = null;
    }
    if (ctTerminalRefreshInterval) {
        clearInterval(ctTerminalRefreshInterval);
        ctTerminalRefreshInterval = null;
    }
}

// ============== Copy Trader Terminal ==============

function classifyCtLogLine(msg) {
    if (msg.includes('[RUN]')) return 'ct-line-run';
    if (msg.includes('[END]')) return 'ct-line-end';
    if (msg.includes('[START]')) return 'ct-line-start';
    if (msg.includes('[STOP]')) return 'ct-line-stop';
    if (msg.includes('[DETECT]')) return 'ct-line-detect';
    if (msg.includes('[TRADE]')) return 'ct-line-trade';
    if (msg.includes('[COPY]')) return 'ct-line-copy';
    if (msg.includes('[DONE]')) return 'ct-line-done';
    if (msg.includes('[SKIP]')) return 'ct-line-skip';
    if (msg.includes('[FAIL]')) return 'ct-line-fail';
    if (msg.includes('[RESULT]')) return 'ct-line-result';
    if (msg.includes('[WAIT]')) return 'ct-line-wait';
    if (msg.startsWith('@')) return 'ct-line-handle';
    if (msg.match(/^\s*(No new|First run)/)) return 'ct-line-muted';
    return '';
}

async function refreshCtTerminal() {
    const body = document.getElementById('ctTerminalBody');
    if (!body) return;

    const res = await api('/api/ct/history?lines=200');
    if (!res.success || !res.logs) return;

    const logs = res.logs;
    if (!logs.length) {
        body.innerHTML = '<div class="ct-terminal-line ct-line-muted">No logs yet. Start the copy trader to see output.</div>';
        return;
    }

    body.innerHTML = logs.map(entry => {
        const time = entry.time || '';
        const msg = entry.message || entry || '';
        const cls = classifyCtLogLine(typeof msg === 'string' ? msg : '');
        const timeHtml = time ? `<span style="color: var(--text-muted); margin-right: 8px;">${escapeHtml(time)}</span>` : '';
        return `<div class="ct-terminal-line ${cls}">${timeHtml}${escapeHtml(typeof msg === 'string' ? msg : JSON.stringify(msg))}</div>`;
    }).join('');

    // Auto-scroll to bottom if auto-tail is on
    if (document.getElementById('ctAutoTail')?.checked) {
        body.scrollTop = body.scrollHeight;
    }
}

function toggleCtAutoTail() {
    const body = document.getElementById('ctTerminalBody');
    if (document.getElementById('ctAutoTail')?.checked && body) {
        body.scrollTop = body.scrollHeight;
    }
}

function clearCtLogs() {
    const body = document.getElementById('ctTerminalBody');
    if (body) body.innerHTML = '<div class="ct-terminal-line ct-line-muted">Logs cleared.</div>';
}

async function loadCopyTrading() {
    const container = document.getElementById('ctConfigsList');

    // Load configs and trades in parallel
    const [configsRes, detectedRes, executedRes] = await Promise.all([
        api('/api/ct/configs'),
        api('/api/ct/detected-trades'),
        api('/api/ct/executed-trades'),
    ]);

    await checkCopyTraderStatus();

    // Render configs
    if (!configsRes.success) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${configsRes.error}</p></div>`;
    } else if (!configsRes.configs.length) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                    <circle cx="9" cy="7" r="4"></circle>
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                    <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
                </svg>
                <p>No traders being followed</p>
                <p style="font-size: 13px; margin-top: 8px;">Enter a Polymarket handle above to start following a trader</p>
            </div>
        `;
    } else {
        container.innerHTML = configsRes.configs.map(c => {
            const statusTag = c.enabled ? '<span class="tag tag-success">ON</span>' : '<span class="tag tag-neutral">OFF</span>';
            const sizingDisplay = `$${c.max_amount.toFixed(1)} max + ${(c.extra_pct * 100).toFixed(0)}% extra`;

            const lastCheck = c.last_check_timestamp
                ? new Date(c.last_check_timestamp * 1000).toLocaleString()
                : 'Never';

            return `
                <div class="card ct-card">
                    <div class="card-header">
                        <div>
                            <div class="card-title">@${escapeHtml(c.handle)}</div>
                            <div class="card-subtitle">${escapeHtml(c.profile_name)}</div>
                            <div class="token-id">${c.wallet_address}</div>
                        </div>
                        <div class="card-actions">
                            ${statusTag}
                            <button class="btn btn-sm btn-outline" onclick="editCopyTrader('${c.id}')">Edit</button>
                            <button class="btn btn-sm btn-outline btn-danger" onclick="deleteCopyTrader('${c.id}')">Delete</button>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="card-stat">
                            <span class="card-stat-label">Max Amount</span>
                            <span class="card-stat-value">$${c.max_amount.toFixed(1)}</span>
                        </div>
                        <div class="card-stat">
                            <span class="card-stat-label">Extra %</span>
                            <span class="card-stat-value">${(c.extra_pct * 100).toFixed(0)}%</span>
                        </div>
                        <div class="card-stat">
                            <span class="card-stat-label">Last Check</span>
                            <span class="card-stat-value" style="font-size: 12px;">${lastCheck}</span>
                        </div>
                        <div class="card-stat">
                            <span class="card-stat-label">Status</span>
                            <span class="card-stat-value">
                                <button class="btn btn-xs ${c.enabled ? 'btn-outline btn-danger' : 'btn-success'}" onclick="toggleCopyTraderEnabled('${c.id}', ${!c.enabled})">
                                    ${c.enabled ? 'Disable' : 'Enable'}
                                </button>
                            </span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }

    // Render detected & executed trades tables
    renderCtTradesTable(detectedRes.success ? detectedRes.trades : [], 'ctDetectedTrades');
    renderCtTradesTable(executedRes.success ? executedRes.trades : [], 'ctExecutedTrades');

    // Update timestamps
    const detectedTs = document.getElementById('ctDetectedTimestamp');
    const executedTs = document.getElementById('ctExecutedTimestamp');
    if (detectedTs) detectedTs.textContent = detectedRes.run_timestamp ? formatRunTimestamp(detectedRes.run_timestamp) : '';
    if (executedTs) executedTs.textContent = executedRes.run_timestamp ? formatRunTimestamp(executedRes.run_timestamp) : '';

    // Load terminal logs
    await refreshCtTerminal();
}

async function addCopyTrader() {
    const handle = document.getElementById('ctHandle').value.trim();
    const maxAmount = parseFloat(document.getElementById('ctMaxAmount').value) || 5;
    const extraPct = parseFloat(document.getElementById('ctExtraPct').value) || 10;

    if (!handle) {
        showToast('Please enter a handle', 'error');
        return;
    }

    const res = await api('/api/ct/add', {
        method: 'POST',
        body: JSON.stringify({
            handle,
            max_amount: maxAmount,
            extra_pct: extraPct,
        })
    });

    if (res.success) {
        showToast(`Now following @${res.config.handle} (${res.config.profile_name})`);
        document.getElementById('ctHandle').value = '';
        loadCopyTrading();
    } else {
        showToast(res.error || 'Failed to add trader', 'error');
    }
}

function editCopyTrader(configId) {
    api(`/api/ct/configs`).then(res => {
        if (!res.success) {
            showToast(res.error, 'error');
            return;
        }

        const c = res.configs.find(cfg => cfg.id === configId);
        if (!c) {
            showToast('Config not found', 'error');
            return;
        }

        showModal('Edit Copy Trader', `
            <div class="form-group">
                <label>Trader</label>
                <div style="padding: 8px 0; color: var(--text-primary);">@${escapeHtml(c.handle)} (${escapeHtml(c.profile_name)})</div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Max Amount ($)</label>
                    <input type="number" class="input" id="editCtMaxAmount" value="${c.max_amount}" min="0.5" step="0.5">
                </div>
                <div class="form-group">
                    <label>Extra %</label>
                    <input type="number" class="input" id="editCtExtraPct" value="${(c.extra_pct * 100).toFixed(0)}" min="0" max="100" step="1">
                </div>
            </div>
            <p style="font-size: 12px; color: var(--text-muted); margin: -8px 0 16px;">Trades under max are copied at exact size. Over max: $max + original &times; extra%.</p>
            <div class="form-group">
                <label>
                    <input type="checkbox" id="editCtEnabled" ${c.enabled ? 'checked' : ''}> Enabled
                </label>
            </div>
            <div class="form-actions">
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" onclick="saveCopyTrader('${configId}')">Save</button>
            </div>
        `);
    });
}

async function saveCopyTrader(configId) {
    const maxAmount = parseFloat(document.getElementById('editCtMaxAmount').value) || 5;
    const extraPct = parseFloat(document.getElementById('editCtExtraPct').value) || 10;
    const enabled = document.getElementById('editCtEnabled').checked;

    const res = await api(`/api/ct/config/${configId}`, {
        method: 'PUT',
        body: JSON.stringify({
            max_amount: maxAmount,
            extra_pct: extraPct,
            enabled,
        })
    });

    if (res.success) {
        showToast('Configuration updated');
        closeModal();
        loadCopyTrading();
    } else {
        showToast(res.error, 'error');
    }
}

async function deleteCopyTrader(configId) {
    if (!confirm('Stop following this trader?')) return;

    const res = await api(`/api/ct/config/${configId}`, { method: 'DELETE' });

    if (res.success) {
        showToast('Trader removed');
        loadCopyTrading();
    } else {
        showToast(res.error, 'error');
    }
}

async function toggleCopyTraderEnabled(configId, enabled) {
    const res = await api(`/api/ct/config/${configId}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled })
    });

    if (res.success) {
        showToast(enabled ? 'Trader enabled' : 'Trader disabled');
        loadCopyTrading();
    } else {
        showToast(res.error, 'error');
    }
}

// Utility
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
