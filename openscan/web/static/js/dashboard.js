/* OpenScanHub - Dashboard */

const dashboard = {
    statusInterval: null,
    multipageSession: null,

    init() {
        this.refreshStatus();
        this.refreshHistory();
        this.refreshProblems();
        this.statusInterval = setInterval(() => {
            this.refreshStatus();
            this.refreshProblems();
        }, 5000);
    },

    setDot(kind) {
        document.getElementById('status-dot').className = `status-dot status-${kind}`;
    },

    async refreshStatus() {
        let data;
        try {
            data = await api.get('api/status');
        } catch (err) {
            this.setDot('offline');
            document.getElementById('scanner-name').textContent = 'OpenScanHub is not responding';
            document.getElementById('scanner-detail').textContent = '';
            return;
        }

        const name = document.getElementById('scanner-name');
        const detail = document.getElementById('scanner-detail');
        const buttons = document.getElementById('scan-buttons');
        const receive = document.getElementById('receive-info');

        if (data.mode === 'scanner') {
            const s = data.scanner || {};
            const st = data.status || {};
            const parts = [];
            if (s.ip) parts.push(s.port && s.port !== 80 ? `${s.ip}:${s.port}` : s.ip);
            if (s.protocol) parts.push(String(s.protocol).toUpperCase());

            name.textContent = s.display_name || s.name || 'Scanner';
            if (data.scan_in_progress || st.state === 'Processing') {
                this.setDot('busy');
                parts.push('Busy');
            } else if (st.state === 'Offline') {
                this.setDot('offline');
                parts.push('Not reachable. Check that the scanner is on.');
            } else {
                this.setDot('online');
                parts.push('Ready');
                if (st.adf_state === 'ScannerAdfLoaded') parts.push('Paper in feeder');
            }
            detail.textContent = parts.join(' · ');

            buttons.classList.remove('hidden');
            receive.classList.add('hidden');
            const sources = Array.isArray(data.sources) ? data.sources : [];
            document.getElementById('btn-adf-group').classList.toggle('hidden', !sources.includes('Feeder'));
            return;
        }

        buttons.classList.add('hidden');
        const rx = data.receivers || {};
        if (data.mode === 'ftp') {
            const ftp = rx.ftp || {};
            name.textContent = 'Receiving scans by FTP';
            detail.textContent = `Port ${ftp.port} · user ${ftp.username || ''}`;
            this.setDot(ftp.running === false ? 'offline' : (ftp.running ? 'online' : 'unknown'));
            receive.textContent = ftp.running === false
                ? 'The FTP receiver is not running. Check Recent problems below, or open Settings.'
                : 'Start the scan on your scanner. Files it sends here are saved automatically.';
            receive.classList.remove('hidden');
        } else if (data.mode === 'folder') {
            const folder = rx.folder || {};
            name.textContent = 'Watching a folder for scans';
            detail.textContent = folder.folder || '';
            this.setDot(folder.running === false ? 'offline' : (folder.running ? 'online' : 'unknown'));
            receive.textContent = 'Files your scanner saves in that folder are moved to your scans folder automatically.';
            receive.classList.remove('hidden');
        } else {
            this.setDot('offline');
            name.textContent = 'No scanner set up yet';
            detail.textContent = '';
            receive.innerHTML = 'Open <a href="settings">Settings</a> to choose a scanner.';
            receive.classList.remove('hidden');
        }
    },

    async refreshHistory() {
        try {
            const data = await api.get('api/history?limit=20');
            const list = document.getElementById('history-list');
            if (!data.scans || data.scans.length === 0) {
                list.innerHTML = `
                    <li class="empty-state">
                        <div class="icon">&#128196;</div>
                        <p>No scans yet</p>
                    </li>`;
                return;
            }
            list.innerHTML = data.scans.map(scan => {
                let state = '<span class="result-ok">Saved</span>';
                if (scan.ok === false && scan.queued) state = '<span class="result-warn">Waiting to retry</span>';
                else if (scan.ok === false) state = '<span class="result-bad">Not saved</span>';
                const autoTag = scan.auto ? ' <span class="badge badge-escl">AUTO</span>' : '';
                const pages = scan.page_count ? ` &middot; ${scan.page_count} pages` : '';
                return `
                    <li class="history-item">
                        <div class="file-icon">&#128196;</div>
                        <div class="file-info">
                            <div class="file-name">${esc(scan.filename)}${autoTag}</div>
                            <div class="file-meta">${esc(formatTime(scan.timestamp))} &middot; ${formatSize(scan.size_bytes)}${pages} &middot; ${state}</div>
                        </div>
                    </li>`;
            }).join('');
        } catch (err) {
            console.error('History refresh failed:', err);
        }
    },

    async refreshProblems() {
        try {
            const [q, e] = await Promise.all([api.get('api/queue'), api.get('api/errors?limit=5')]);
            const pending = q.pending || [];
            document.getElementById('queue-card').classList.toggle('hidden', pending.length === 0);
            document.getElementById('queue-list').innerHTML = pending.map(item => `
                <li><span class="mono">${esc(String(item.file || '').split(/[\\/]/).pop())}</span>
                    to ${esc(item.destination)}${item.attempts ? `, tried ${item.attempts} times` : ''}
                    ${item.last_error ? `<br><span class="when">${esc(item.last_error)}</span>` : ''}</li>`).join('');

            const errors = e.errors || [];
            document.getElementById('errors-card').classList.toggle('hidden', errors.length === 0);
            document.getElementById('errors-list').innerHTML = errors.map(err => `
                <li><span class="when">${esc(formatTime(err.time))}</span>${esc(err.message)}</li>`).join('');
        } catch (err) {
            console.error('Problem refresh failed:', err);
        }
    },

    async retryAll() {
        const data = await api.post('api/queue/retry', {});
        if (data._status === 200) {
            toast(`Trying again (${data.retried || 0})`, 'info');
        } else {
            toast(data.error || 'Could not retry', 'error');
        }
        setTimeout(() => this.refreshProblems(), 1500);
    },

    async scan(source) {
        const overlay = document.getElementById('scan-overlay');
        overlay.classList.add('active');
        document.getElementById('scan-status-text').textContent = 'Please wait...';
        try {
            const data = await api.post('api/scan', { source });
            scanResultToast(data);
            this.refreshHistory();
            this.refreshProblems();
        } catch (err) {
            toast(`Error: ${err.message}`, 'error');
        } finally {
            overlay.classList.remove('active');
        }
    },

    // --- Several pages into one file ---

    async startMultipage(source) {
        try {
            const data = await api.post('api/multipage/start', { source });
            if (data.session_id) {
                this.multipageSession = { sessionId: data.session_id, source, pageCount: 0 };
                document.getElementById('multipage-overlay').classList.add('active');
                this.updateMultipagePreview();
            } else {
                toast(data.error || 'Could not start', 'error');
            }
        } catch (err) {
            toast(`Error: ${err.message}`, 'error');
        }
    },

    async scanMultipagePage() {
        if (!this.multipageSession) return;
        const scanning = document.getElementById('multipage-scanning');
        const scanBtn = document.getElementById('btn-scan-page');
        const saveBtn = document.getElementById('btn-save-multipage');
        scanning.classList.add('active');
        scanBtn.disabled = true;
        saveBtn.disabled = true;
        try {
            const data = await api.post('api/multipage/scan', { session_id: this.multipageSession.sessionId });
            if (data._status === 409) {
                toast('The scanner is busy with another scan. Try again in a moment.', 'warning');
            } else if (data.success) {
                this.multipageSession.pageCount = data.page_count;
                this.updateMultipagePreview();
                toast(`Page ${data.page_count} scanned`, 'success', 2500);
            } else {
                toast(`Scan failed: ${data.error}`, 'error');
            }
        } catch (err) {
            toast(`Error: ${err.message}`, 'error');
        } finally {
            scanning.classList.remove('active');
            scanBtn.disabled = false;
            saveBtn.disabled = !this.multipageSession || this.multipageSession.pageCount === 0;
        }
    },

    async saveMultipage() {
        if (!this.multipageSession || this.multipageSession.pageCount === 0) return;
        const scanning = document.getElementById('multipage-scanning');
        scanning.classList.add('active');
        try {
            const data = await api.post('api/multipage/save', { session_id: this.multipageSession.sessionId });
            scanResultToast(data);
            if (data.success || data.queued) this.hideMultipageModal();
            this.refreshHistory();
            this.refreshProblems();
        } catch (err) {
            toast(`Error: ${err.message}`, 'error');
        } finally {
            scanning.classList.remove('active');
        }
    },

    async cancelMultipage() {
        if (this.multipageSession) {
            try {
                await api.post('api/multipage/cancel', { session_id: this.multipageSession.sessionId });
            } catch (err) {
                console.error('Cancel failed:', err);
            }
        }
        this.hideMultipageModal();
    },

    hideMultipageModal() {
        document.getElementById('multipage-overlay').classList.remove('active');
        document.getElementById('multipage-scanning').classList.remove('active');
        this.multipageSession = null;
        document.getElementById('btn-save-multipage').disabled = true;
    },

    updateMultipagePreview() {
        const area = document.getElementById('multipage-preview-area');
        const count = this.multipageSession?.pageCount || 0;
        document.getElementById('multipage-count').textContent = `${count} page${count !== 1 ? 's' : ''} scanned`;
        document.getElementById('btn-save-multipage').disabled = count === 0;
        if (count === 0) {
            area.innerHTML = `
                <div class="empty-state">
                    <div class="icon">&#128196;</div>
                    <p>No pages yet</p>
                </div>`;
            return;
        }
        const sid = encodeURIComponent(this.multipageSession.sessionId);
        area.innerHTML = Array.from({ length: count }, (_, i) => `
            <div class="multipage-thumb">
                <img src="api/multipage/thumbnail/${sid}/${i}" alt="Page ${i + 1}">
                <span class="thumb-label">Page ${i + 1}</span>
            </div>`).join('');
    },
};

document.addEventListener('DOMContentLoaded', () => dashboard.init());
