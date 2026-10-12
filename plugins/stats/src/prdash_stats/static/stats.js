// Initialize charts after HTMX loads the stats content
document.body.addEventListener('htmx:afterSwap', function(event) {
    if (event.detail.target.id === 'stats-content') {
        initializeCharts();
    }
});

if (document.getElementById('velocityChart')) {
    initializeCharts();
}

// Update active state on time filter buttons and show loading
document.body.addEventListener('htmx:beforeRequest', function(event) {
    if (event.detail.target && event.detail.target.id === 'stats-content') {
        // Update button active states
        const clickedBtn = event.detail.elt;
        if (clickedBtn && clickedBtn.closest('.time-filter')) {
            document.querySelectorAll('.time-filter .btn').forEach(btn => {
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-secondary');
            });
            clickedBtn.classList.remove('btn-secondary');
            clickedBtn.classList.add('btn-primary');

            // Keep refresh button URL in sync with selected time period
            const refreshBtn = document.getElementById('stats-refresh-btn');
            const filterUrl = clickedBtn.getAttribute('hx-get');
            if (refreshBtn && filterUrl) {
                refreshBtn.setAttribute('hx-get', filterUrl);
                htmx.process(refreshBtn);
            }
        }

        // Show loading skeleton
        const statsContent = document.getElementById('stats-content');
        if (statsContent) {
            statsContent.innerHTML = `
                <div class="stats-loading">
                    <div class="quick-stats-grid">
                        <div class="stat-card skeleton-card"><div class="skeleton skeleton-value"></div><div class="skeleton skeleton-label"></div></div>
                        <div class="stat-card skeleton-card"><div class="skeleton skeleton-value"></div><div class="skeleton skeleton-label"></div></div>
                        <div class="stat-card skeleton-card"><div class="skeleton skeleton-value"></div><div class="skeleton skeleton-label"></div></div>
                        <div class="stat-card skeleton-card"><div class="skeleton skeleton-value"></div><div class="skeleton skeleton-label"></div></div>
                    </div>
                    <div class="stats-row"><div class="stats-section stats-section-wide skeleton-section"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-chart"></div></div></div>
                    <div class="stats-row stats-row-2col">
                        <div class="stats-section skeleton-section"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-content"></div></div>
                        <div class="stats-section skeleton-section"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-content"></div></div>
                    </div>
                    <div class="stats-row stats-row-2col">
                        <div class="stats-section skeleton-section"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-content"></div></div>
                        <div class="stats-section skeleton-section"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-content"></div></div>
                    </div>
                    <div class="stats-loading-text"><div class="loading-spinner"></div><span>Loading statistics...</span></div>
                </div>
            `;
        }
    }
});

function initializeCharts() {
    // Get theme-aware colors
    function getChartColors() {
        const style = getComputedStyle(document.documentElement);
        return {
            primary: style.getPropertyValue('--color-primary').trim() || '#0969da',
            success: style.getPropertyValue('--color-success').trim() || '#1a7f37',
            danger: style.getPropertyValue('--color-danger').trim() || '#cf222e',
            text: style.getPropertyValue('--color-text').trim() || '#24292f',
            textSecondary: style.getPropertyValue('--color-text-secondary').trim() || '#57606a',
            border: style.getPropertyValue('--color-border').trim() || '#d0d7de',
            bgSecondary: style.getPropertyValue('--color-bg-secondary').trim() || '#f6f8fa',
        };
    }

    let colors = getChartColors();
    let charts = [];

    // Common chart options
    const commonOptions = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                labels: {
                    color: colors.text,
                    font: { size: 12 }
                }
            }
        },
        scales: {
            x: {
                ticks: { color: colors.textSecondary },
                grid: { color: colors.border, drawBorder: false }
            },
            y: {
                ticks: { color: colors.textSecondary },
                grid: { color: colors.border, drawBorder: false },
                beginAtZero: true
            }
        }
    };

    // Velocity Chart (PRs over time)
    const velocityCtx = document.getElementById('velocityChart');
    if (velocityCtx) {
        const velocityData = JSON.parse(document.getElementById('velocityData').textContent);
        const velocityChart = new Chart(velocityCtx, {
            type: 'line',
            data: {
                labels: velocityData.labels,
                datasets: [
                    {
                        label: 'Opened',
                        data: velocityData.opened,
                        borderColor: colors.primary,
                        backgroundColor: colors.primary + '20',
                        tension: 0.3,
                        fill: true
                    },
                    {
                        label: 'Merged',
                        data: velocityData.merged,
                        borderColor: colors.success,
                        backgroundColor: colors.success + '20',
                        tension: 0.3,
                        fill: true
                    }
                ]
            },
            options: {
                ...commonOptions,
                plugins: {
                    ...commonOptions.plugins,
                    title: {
                        display: true,
                        text: 'PR Activity Over Time',
                        color: colors.text
                    }
                }
            }
        });
        charts.push(velocityChart);
    }

    // Lines Changed Chart
    const linesCtx = document.getElementById('linesChart');
    if (linesCtx) {
        const velocityData = JSON.parse(document.getElementById('velocityData').textContent);
        const linesChart = new Chart(linesCtx, {
            type: 'bar',
            data: {
                labels: velocityData.labels,
                datasets: [
                    {
                        label: 'Lines Added',
                        data: velocityData.linesAdded,
                        backgroundColor: colors.success + '80',
                        borderColor: colors.success,
                        borderWidth: 1
                    },
                    {
                        label: 'Lines Removed',
                        data: velocityData.linesRemoved,
                        backgroundColor: colors.danger + '80',
                        borderColor: colors.danger,
                        borderWidth: 1
                    }
                ]
            },
            options: {
                ...commonOptions,
                plugins: {
                    ...commonOptions.plugins,
                    title: {
                        display: true,
                        text: 'Lines Changed Over Time',
                        color: colors.text
                    }
                },
                scales: {
                    ...commonOptions.scales,
                    x: { ...commonOptions.scales.x, stacked: false },
                    y: { ...commonOptions.scales.y, stacked: false }
                }
            }
        });
        charts.push(linesChart);
    }

    // Repo Distribution Pie Chart
    const repoCtx = document.getElementById('repoChart');
    if (repoCtx) {
        const repoData = JSON.parse(document.getElementById('repoData').textContent);
        const repoChart = new Chart(repoCtx, {
            type: 'doughnut',
            data: {
                labels: repoData.labels,
                datasets: [{
                    data: repoData.values,
                    backgroundColor: [
                        colors.primary,
                        colors.success,
                        colors.danger,
                        '#8250df',
                        '#bf8700',
                        '#0550ae',
                        '#1a7f37',
                        '#cf222e',
                    ],
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'right',
                        labels: {
                            color: colors.text,
                            font: { size: 11 },
                            boxWidth: 12,
                            padding: 8
                        }
                    }
                }
            }
        });
        charts.push(repoChart);
    }

    // Update charts on theme change
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (mutation.attributeName === 'data-theme') {
                colors = getChartColors();
                charts.forEach(chart => {
                    // Update colors
                    if (chart.options.plugins?.legend?.labels) {
                        chart.options.plugins.legend.labels.color = colors.text;
                    }
                    if (chart.options.scales?.x) {
                        chart.options.scales.x.ticks.color = colors.textSecondary;
                        chart.options.scales.x.grid.color = colors.border;
                    }
                    if (chart.options.scales?.y) {
                        chart.options.scales.y.ticks.color = colors.textSecondary;
                        chart.options.scales.y.grid.color = colors.border;
                    }
                    chart.update();
                });
            }
        });
    });

    observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme']
    });
}

document.addEventListener('click', function(event) {
    const button = event.target.closest('.health-stat[data-toggle]');
    if (!button) return;
    const target = document.getElementById(button.getAttribute('data-toggle'));
    const icon = button.querySelector('.health-stat-icon');
    if (!target) return;
    const opening = target.style.display === 'none';
    target.style.display = opening ? 'block' : 'none';
    if (icon) icon.textContent = opening ? '\u2212' : '+';
    button.classList.toggle('active', opening);
});
