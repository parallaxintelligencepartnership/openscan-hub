/* OpenScanHub - First-run setup (three steps) */

const wizard = {
    step: 1,
    scanner: null,      // a found or probed scanner
    pushMode: null,     // 'ftp' | 'folder' | null
    defaults: {},
    config: {},
    secrets: {},
    lanInitially: false,

    async init() {
        try {
            [this.defaults, this.config] = await Promise.all([api.get('api/defaults'), api.get('api/config')]);
        } catch (e) {
            this.defaults = {};
            this.config = {};
        }
        const folder = document.getElementById('output-folder');
        folder.value = this.defaults.output_folder || '';
        folder.placeholder = this.defaults.default_output_folder || '';
        document.getElementById('folder-hint').textContent = this.folderHint();
        document.getElementById('watch-folder').placeholder = this.defaults.platform === 'Windows'
            ? '\\\\NAS\\ScannerInbox' : '/mnt/scanner-inbox';

        const bind = (this.config.web || {}).bind || '127.0.0.1';
        this.lanInitially = !(bind === '127.0.0.1' || bind === 'localhost' || bind === '::1');
        document.getElementById('allow-lan').checked = this.lanInitially;
        this.discover();
    },

    folderHint() {
        if (this.defaults.is_docker) {
            return 'Inside Docker, /scans is the folder you mapped in docker-compose.yml. ' +
                   'The folder is created if it does not exist.';
        }
        if (this.defaults.platform === 'Windows') {
            return 'To save on a NAS, type its network path, for example \\\\NAS\\Scans. ' +
                   'The folder is created if it does not exist.';
        }
        return 'To save on a NAS, use the folder where the share is mounted, for example ' +
               (this.defaults.platform === 'Darwin' ? '/Volumes/Scans' : '/mnt/nas/scans') +
               '. The folder is created if it does not exist.';
    },

    show(step) {
        this.step = step;
        document.querySelectorAll('.step-dot').forEach(dot => {
            const s = parseInt(dot.dataset.step, 10);
            dot.classList.toggle('active', s === step);
            dot.classList.toggle('completed', s < step);
            dot.textContent = s < step ? '✓' : String(s);
        });
        document.querySelectorAll('.step-content').forEach(c => {
            c.classList.toggle('active', parseInt(c.dataset.step, 10) === step);
        });
        window.scrollTo(0, 0);
    },

    next() { if (this.step === 1 && this.validateStep1()) this.show(2); },
    back() { if (this.step > 1) this.show(this.step - 1); },

    showError(id, message) {
        const el = document.getElementById(id);
        el.textContent = message || '';
        el.classList.toggle('hidden', !message);
    },

    updateNext() {
        document.getElementById('btn-step1-next').disabled = !(this.scanner || this.pushMode);
        const hasFeeder = !!(this.scanner && (this.scanner.sources || []).includes('Feeder'));
        document.getElementById('feeder-option').classList.toggle('hidden', !hasFeeder);
        document.getElementById('watch-folder-group').classList.toggle('hidden', this.pushMode !== 'folder');
    },

    validateStep1() {
        if (this.pushMode === 'folder') {
            const watch = document.getElementById('watch-folder').value.trim();
            if (!watch) {
                this.showError('step1-error', 'Please enter the folder your scanner saves into.');
                return false;
            }
        }
        this.showError('step1-error', '');
        return !!(this.scanner || this.pushMode);
    },

    // --- Step 1 ---

    async discover() {
        const btn = document.getElementById('btn-discover');
        const loading = document.getElementById('discover-loading');
        const list = document.getElementById('scanner-list');
        btn.classList.add('hidden');
        loading.classList.remove('hidden');
        document.getElementById('no-scanners').classList.add('hidden');
        list.innerHTML = '';
        try {
            const data = await api.post('api/discover', { timeout: 5 });
            const scanners = data.scanners || [];
            scanners.forEach(scanner => {
                const li = document.createElement('li');
                li.className = 'scanner-item';
                li.onclick = () => this.selectScanner(scanner, li);
                const proto = String(scanner.protocol || '');
                li.innerHTML = `
                    <div class="icon">&#128424;</div>
                    <div class="info">
                        <div class="name">${esc(scanner.display_name || scanner.name)}</div>
                        <div class="detail">${esc(scanner.ip)}</div>
                    </div>
                    <span class="badge badge-${esc(proto)}">${esc(proto.toUpperCase())}</span>`;
                list.appendChild(li);
            });
            document.getElementById('no-scanners').classList.toggle('hidden', scanners.length > 0);
            if (scanners.length === 1) this.selectScanner(scanners[0], list.firstChild);
        } catch (err) {
            list.innerHTML = `<div class="alert alert-error">Search failed: ${esc(err.message)}</div>`;
        } finally {
            loading.classList.add('hidden');
            btn.classList.remove('hidden');
        }
    },

    clearPush() {
        this.pushMode = null;
        document.querySelectorAll('input[name="push-mode"]').forEach(r => { r.checked = false; });
    },

    selectScanner(scanner, element) {
        this.scanner = scanner;
        this.clearPush();
        document.querySelectorAll('.scanner-item').forEach(el => el.classList.remove('selected'));
        if (element) element.classList.add('selected');
        this.updateNext();
    },

    async probeManual() {
        const ip = document.getElementById('manual-ip').value.trim();
        const result = document.getElementById('probe-result');
        if (!ip) {
            result.innerHTML = '<div class="alert alert-error">Please enter an IP address</div>';
            return;
        }
        result.innerHTML = '<div class="loading"><div class="spinner"></div><span>Checking...</span></div>';
        try {
            const data = await api.post('api/probe', { ip });
            if (data.found) {
                this.selectScanner(data.scanner, null);
                result.innerHTML = `<div class="alert alert-success">Found ${esc(data.scanner.display_name || data.scanner.name)}</div>`;
            } else {
                result.innerHTML = `<div class="alert alert-warning">${esc(data.error || 'No scanner answered at that address')}</div>`;
            }
        } catch (err) {
            result.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
        }
    },

    selectPush(mode) {
        this.pushMode = mode;
        this.scanner = null;
        document.querySelectorAll('.scanner-item').forEach(el => el.classList.remove('selected'));
        this.updateNext();
    },

    // --- Step 2 ---

    togglePaperless() {
        const on = document.getElementById('paperless-enabled').checked;
        document.getElementById('paperless-config').classList.toggle('hidden', !on);
    },

    togglePaperlessMode() {
        const mode = document.getElementById('paperless-mode').value;
        document.getElementById('paperless-consume-fields').classList.toggle('hidden', mode !== 'consume');
        document.getElementById('paperless-api-fields').classList.toggle('hidden', mode !== 'api');
    },

    async testPaperless() {
        const url = document.getElementById('paperless-url').value.trim();
        const token = document.getElementById('paperless-token').value.trim();
        const result = document.getElementById('paperless-test-result');
        if (!url) {
            result.innerHTML = '<div class="alert alert-error">Please enter the Paperless-NGX address</div>';
            return;
        }
        result.innerHTML = '<div class="loading"><div class="spinner"></div><span>Testing...</span></div>';
        const data = await api.post('api/test-paperless', { url, token });
        result.innerHTML = data.ok
            ? '<div class="alert alert-success">Connected to Paperless-NGX</div>'
            : `<div class="alert alert-error">Could not connect: ${esc(data.error)}</div>`;
    },

    buildConfig() {
        const s = this.scanner || {};
        const off = { enabled: false };
        const body = {
            output: { folder: document.getElementById('output-folder').value.trim() },
        };
        if (this.pushMode === 'ftp') {
            body.scanner = { ip: '', port: 0, protocol: 'ftp', name: '', model: '', path: '' };
            body.ftp_receive = { enabled: true };
            body.folder_watch = off;
            body.monitor = off;
        } else if (this.pushMode === 'folder') {
            body.scanner = { ip: '', port: 0, protocol: 'folder', name: '', model: '', path: '' };
            body.folder_watch = { enabled: true, watch_folder: document.getElementById('watch-folder').value.trim() };
            body.ftp_receive = off;
            body.monitor = off;
        } else {
            body.scanner = {
                ip: s.ip || '', port: s.port || 80, protocol: s.protocol || 'escl',
                name: s.name || '', model: s.model || s.display_name || '', path: s.path || '',
            };
            body.monitor = { enabled: document.getElementById('auto-feeder').checked &&
                                      (s.sources || []).includes('Feeder') };
            body.ftp_receive = off;
            body.folder_watch = off;
        }
        if (document.getElementById('paperless-enabled').checked) {
            body.paperless = {
                enabled: true,
                mode: document.getElementById('paperless-mode').value,
                consume_folder: document.getElementById('paperless-consume-folder').value.trim(),
                api_url: document.getElementById('paperless-url').value.trim(),
                api_token: document.getElementById('paperless-token').value.trim(),
            };
        } else {
            body.paperless = off;
        }
        return body;
    },

    sameFolder(a, b) {
        const norm = p => String(p || '').trim().replace(/[\\/]+$/, '').toLowerCase();
        return norm(a) !== '' && norm(a) === norm(b);
    },

    async save() {
        const body = this.buildConfig();
        if (!body.output.folder) {
            this.showError('step2-error', 'Please choose a folder for your scans.');
            return;
        }
        if (this.pushMode === 'folder' && this.sameFolder(body.output.folder, body.folder_watch.watch_folder)) {
            this.showError('step2-error', 'The folder your scanner saves into must be different from this folder.');
            return;
        }
        this.showError('step2-error', '');
        const btn = document.getElementById('btn-save');
        btn.disabled = true;
        try {
            const data = await api.post('api/save-config', body);
            if (!data.saved) {
                this.showError('step2-error', data.error || 'Could not save your settings.');
                return;
            }
            (data.warnings || []).forEach(w => toast(w, 'warning', 8000));
            await this.prepareDone(body);
            this.show(3);
        } catch (err) {
            this.showError('step2-error', err.message);
        } finally {
            btn.disabled = false;
        }
    },

    // --- Step 3 ---

    async prepareDone(body) {
        try { this.secrets = await api.get('api/secrets'); } catch (e) { this.secrets = {}; }
        const items = [];
        if (this.pushMode === 'ftp') items.push(['Scanner', 'Sends files by FTP']);
        else if (this.pushMode === 'folder') items.push(['Scanner', `Saves into ${body.folder_watch.watch_folder}`]);
        else items.push(['Scanner', (this.scanner.display_name || this.scanner.name || this.scanner.ip)]);
        if (body.monitor && body.monitor.enabled) items.push(['Feeder', 'Scans automatically when paper is loaded']);
        items.push(['Scans go to', body.output.folder]);
        items.push(['Paperless-NGX', body.paperless.enabled ? 'Also sends there' : 'Not used']);
        document.getElementById('config-summary').innerHTML = items.map(([k, v]) =>
            `<li><span class="label">${esc(k)}</span><span class="value">${esc(v)}</span></li>`).join('');

        document.getElementById('test-scan-block').classList.toggle('hidden', !!this.pushMode);
        document.getElementById('ftp-block').classList.toggle('hidden', this.pushMode !== 'ftp');
        if (this.pushMode === 'ftp') {
            const cfg = await api.get('api/config');
            const ftp = cfg.ftp_receive || {};
            const rows = [
                ['Server', this.defaults.lan_ip || "this computer's IP address"],
                ['Port', ftp.port],
                ['User name', ftp.username],
                ['Password', this.secrets.ftp_password || ''],
                ['Folder', '/'],
            ];
            document.getElementById('ftp-summary').innerHTML = rows.map(([k, v]) =>
                `<li><span class="label">${esc(k)}</span><span class="value mono">${esc(v)}</span></li>`).join('');
        }
        document.getElementById('lan-token').textContent = this.secrets.api_token || '';
        this.toggleLan();
    },

    toggleLan() {
        document.getElementById('lan-info').classList.toggle('hidden', !document.getElementById('allow-lan').checked);
    },

    copyToken() { copyText(this.secrets.api_token || ''); },

    async testScan() {
        const btn = document.getElementById('btn-test-scan');
        const loading = document.getElementById('test-loading');
        const result = document.getElementById('test-result');
        btn.classList.add('hidden');
        loading.classList.remove('hidden');
        result.innerHTML = '';
        try {
            const data = await api.post('api/scan', { source: 'flatbed' });
            let cls = 'alert-error';
            let text = data.message || data.error || 'Scan failed';
            if (data._status === 409) { cls = 'alert-warning'; text = 'The scanner is busy. Try again in a moment.'; }
            else if (data.success) {
                cls = 'alert-success';
                text = `It works. Saved to ${(data.saved_paths || [])[0] || data.filename}`;
            } else if (data.queued) { cls = 'alert-warning'; }
            result.innerHTML = `<div class="alert ${cls}">${esc(text)}</div>`;
        } catch (err) {
            result.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
        } finally {
            loading.classList.add('hidden');
            btn.classList.remove('hidden');
        }
    },

    async finish() {
        const lan = document.getElementById('allow-lan').checked;
        if (lan !== this.lanInitially) {
            const data = await api.post('api/settings', { web: { bind: lan ? '0.0.0.0' : '127.0.0.1' } });
            if (!data.saved) {
                toast(data.error || 'Could not save the network setting', 'error');
                return;
            }
        }
        window.location.href = 'dashboard';
    },
};

document.addEventListener('DOMContentLoaded', () => wizard.init());
