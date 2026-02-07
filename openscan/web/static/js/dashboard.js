/* OpenScanHub - Dashboard Logic */

const dashboard = {
    statusInterval: null,

    init() {
        this.refreshStatus();
        this.refreshHistory();
        // Poll status every 5 seconds
        this.statusInterval = setInterval(() => this.refreshStatus(), 5000);
    },

    async refreshStatus() {
        try {
            const resp = await fetch('/api/status');
            const data = await resp.json();

            const dot = document.getElementById('status-dot');
            const name = document.getElementById('scanner-name');
            const detail = document.getElementById('scanner-detail');

            if (!data.configured) {
                dot.className = 'status-dot status-offline';
                name.textContent = 'No scanner configured';
                detail.textContent = '';
                return;
            }

            const scanner = data.scanner || {};
            const status = data.status || {};

            name.textContent = scanner.display_name || scanner.name || 'Scanner';
            detail.textContent = `${scanner.ip}:${scanner.port} \u00B7 ${(scanner.protocol || '').toUpperCase()}`;

            if (status.state === 'Idle') {
                dot.className = 'status-dot status-online';
                if (status.adf_state === 'ScannerAdfLoaded') {
                    detail.textContent += ' \u00B7 Paper in feeder';
                }
            } else if (status.state === 'Processing') {
                dot.className = 'status-dot status-busy';
                detail.textContent += ' \u00B7 Busy';
            } else {
                dot.className = 'status-dot status-offline';
                detail.textContent += ' \u00B7 Offline';
            }

            // Show/hide scan buttons based on capabilities
            const sources = scanner.sources || ['Platen'];
            document.getElementById('btn-adf').classList.toggle('hidden', !sources.includes('Feeder'));

        } catch (err) {
            console.error('Status refresh failed:', err);
        }
    },

    async refreshHistory() {
        try {
            const resp = await fetch('/api/history?limit=20');
            const data = await resp.json();
            const list = document.getElementById('history-list');

            if (!data.scans || data.scans.length === 0) {
                list.innerHTML = `
                    <li class="empty-state">
                        <div class="icon">&#128196;</div>
                        <p>No scans yet</p>
                    </li>
                `;
                return;
            }

            list.innerHTML = data.scans.map(scan => {
                const date = new Date(scan.timestamp);
                const timeStr = date.toLocaleString();
                const sizeStr = this.formatSize(scan.size_bytes);
                const autoTag = scan.auto ? ' <span class="badge badge-escl">AUTO</span>' : '';

                return `
                    <li class="history-item">
                        <div class="file-icon">&#128196;</div>
                        <div class="file-info">
                            <div class="file-name">${this.esc(scan.filename)}${autoTag}</div>
                            <div class="file-meta">${timeStr} &middot; ${sizeStr}</div>
                        </div>
                    </li>
                `;
            }).join('');
        } catch (err) {
            console.error('History refresh failed:', err);
        }
    },

    async scan(type) {
        const overlay = document.getElementById('scan-overlay');
        overlay.classList.add('active');
        document.getElementById('scan-status-text').textContent = 'Please wait...';

        try {
            const resp = await fetch(`/api/scan/${type}`);
            const data = await resp.json();

            overlay.classList.remove('active');

            if (data.success) {
                this.toast(`Scanned: ${data.filename} (${this.formatSize(data.size_bytes)})`, 'success');
                this.refreshHistory();
            } else {
                this.toast(`Scan failed: ${data.error}`, 'error');
            }
        } catch (err) {
            overlay.classList.remove('active');
            this.toast(`Error: ${err.message}`, 'error');
        }
    },

    toast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100px)';
            toast.style.transition = 'all 0.3s';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    },

    formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    },

    esc(str) {
        if (str == null) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    },
};

// Init on page load
document.addEventListener('DOMContentLoaded', () => dashboard.init());
