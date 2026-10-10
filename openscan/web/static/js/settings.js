/* OpenScanHub - Settings. Prefilled from the current config; blank secrets keep their value. */

const $ = id => document.getElementById(id);

const settings = {
    cfg: {},
    defaults: {},
    secrets: null,
    newScanner: null,
    tokenShown: false,

    async init() {
        [this.cfg, this.defaults] = await Promise.all([api.get('api/config'), api.get('api/defaults')]);
        this.secrets = await api.get('api/secrets');
        this.fill();
        this.refreshProblems();
    },

    isLan(bind) { return !['127.0.0.1', 'localhost', '::1', ''].includes(bind || ''); },

    fill() {
        const c = this.cfg;
        this.showScanner(c.scanner);
        $('monitor-enabled').checked = !!c.monitor.enabled;

        $('output-folder').value = c.output.folder || '';
        $('output-folder').placeholder = this.defaults.default_output_folder || '';
        $('folder-hint').textContent = this.defaults.platform === 'Windows'
            ? 'A NAS works too: type its network path, for example \\\\NAS\\Scans.'
            : 'A NAS works too: use the folder where the share is mounted.';
        $('filename-pattern').value = c.output.filename_pattern || '';
        this.setSelect('scan-resolution', String(c.scan.resolution));
        $('scan-color').value = c.scan.color_mode;
        $('scan-page-size').value = c.scan.page_size;
        $('scan-duplex').checked = !!c.scan.duplex;
        $('scan-remove-blank').checked = !!c.scan.remove_blank_pages;

        const p = c.paperless;
        $('paperless-enabled').checked = !!p.enabled;
        $('paperless').open = !!p.enabled;
        $('paperless-mode').value = p.mode;
        $('paperless-consume-folder').value = p.consume_folder || '';
        $('paperless-url').value = p.api_url || '';
        $('paperless-token').value = '';
        $('paperless-token').placeholder = p.has_token ? 'Saved. Leave blank to keep it.' : '';
        $('paperless-token-hint').textContent = 'In Paperless-NGX, open your profile (top right) to find or create the token.';
        $('paperless-tags').value = (p.tags || []).join(', ');
        $('paperless-correspondent').value = p.correspondent || '';
        $('paperless-document-type').value = p.document_type || '';
        $('paperless-keep-local').checked = !!p.keep_local_copy;
        this.togglePaperlessMode();

        const f = c.ftp_receive;
        $('ftp-enabled').checked = !!f.enabled;
        $('ftp-server').textContent = this.defaults.lan_ip || "this computer's IP address";
        $('ftp-port').value = f.port;
        $('ftp-username').value = f.username;
        $('ftp-password').value = (this.secrets && this.secrets.ftp_password) || '';
        $('folder-enabled').checked = !!c.folder_watch.enabled;
        $('watch-folder').value = c.folder_watch.watch_folder || '';

        $('ha-enabled').checked = !!c.homeassistant.enabled;

        $('allow-lan').checked = this.isLan(c.web.bind);
        $('mdns-enabled').checked = !!c.web.advertise_mdns;
        $('web-port').value = c.web.port;
        $('restart-alert').classList.toggle('hidden', !c.restart_needed);

        $('log-folder').textContent = c.log_folder || 'not available';
        $('btn-open-logs').classList.toggle('hidden', !(c.local_request && !c.is_docker && c.log_folder));
    },

    setSelect(id, value) {
        const sel = $(id);
        if (![...sel.options].some(o => o.value === value)) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = `${value} dpi`;
            sel.appendChild(opt);
        }
        sel.value = value;
    },

    showScanner(s) {
        let text;
        if (s.protocol === 'ftp') text = 'Your scanner sends files by FTP.';
        else if (s.protocol === 'folder') text = 'Your scanner saves files into a watched folder.';
        else if (s.ip) text = `${s.model || s.name || 'Scanner'} at ${s.ip} (${String(s.protocol).toUpperCase()})`;
        else text = 'No scanner chosen yet.';
        $('scanner-current').textContent = text;
    },

    togglePaperlessMode() {
        const mode = $('paperless-mode').value;
        $('paperless-consume-fields').classList.toggle('hidden', mode !== 'consume');
        $('paperless-api-fields').classList.toggle('hidden', mode !== 'api');
    },

    async discover() {
        $('discover-loading').classList.remove('hidden');
        const list = $('scanner-list');
        list.innerHTML = '';
        try {
            const data = await api.post('api/discover', { timeout: 5 });
            const scanners = data.scanners || [];
            if (scanners.length === 0) {
                list.innerHTML = '<div class="alert alert-warning">No scanner found. Try entering its IP address.</div>';
            }
            scanners.forEach(sc => {
                const li = document.createElement('li');
                li.className = 'scanner-item';
                li.innerHTML = `<div class="info"><div class="name">${esc(sc.display_name || sc.name)}</div>
                    <div class="detail">${esc(sc.ip)}</div></div>
                    <span class="badge badge-${esc(sc.protocol)}">${esc(String(sc.protocol).toUpperCase())}</span>`;
                li.onclick = () => this.pickScanner(sc, li);
                list.appendChild(li);
            });
        } finally {
            $('discover-loading').classList.add('hidden');
        }
    },

    async probeManual() {
        const ip = $('manual-ip').value.trim();
        const result = $('probe-result');
        if (!ip) return;
        result.innerHTML = '<div class="loading"><div class="spinner"></div><span>Checking...</span></div>';
        const data = await api.post('api/probe', { ip });
        if (data.found) {
            this.pickScanner(data.scanner, null);
            result.innerHTML = `<div class="alert alert-success">Found ${esc(data.scanner.display_name || data.scanner.name)}. Click Save to use it.</div>`;
        } else {
            result.innerHTML = `<div class="alert alert-warning">${esc(data.error || 'No scanner answered')}</div>`;
        }
    },

    pickScanner(sc, li) {
        this.newScanner = sc;
        document.querySelectorAll('.scanner-item').forEach(el => el.classList.remove('selected'));
        if (li) li.classList.add('selected');
        this.showScanner({ ...sc, model: sc.model || sc.display_name });
    },

    collect() {
        const body = {
            monitor: { enabled: $('monitor-enabled').checked },
            output: { folder: $('output-folder').value.trim(), filename_pattern: $('filename-pattern').value.trim() },
            scan: {
                resolution: parseInt($('scan-resolution').value, 10),
                color_mode: $('scan-color').value,
                page_size: $('scan-page-size').value,
                duplex: $('scan-duplex').checked,
                remove_blank_pages: $('scan-remove-blank').checked,
            },
            paperless: {
                enabled: $('paperless-enabled').checked,
                mode: $('paperless-mode').value,
                consume_folder: $('paperless-consume-folder').value.trim(),
                api_url: $('paperless-url').value.trim(),
                api_token: $('paperless-token').value.trim(),
                tags: $('paperless-tags').value,
                correspondent: $('paperless-correspondent').value.trim(),
                document_type: $('paperless-document-type').value.trim(),
                keep_local_copy: $('paperless-keep-local').checked,
            },
            ftp_receive: {
                enabled: $('ftp-enabled').checked,
                port: parseInt($('ftp-port').value, 10),
                username: $('ftp-username').value.trim(),
                password: $('ftp-password').value.trim(),
            },
            folder_watch: { enabled: $('folder-enabled').checked, watch_folder: $('watch-folder').value.trim() },
            homeassistant: { enabled: $('ha-enabled').checked },
            web: { advertise_mdns: $('mdns-enabled').checked, port: parseInt($('web-port').value, 10) },
        };
        const lan = $('allow-lan').checked;
        if (lan !== this.isLan(this.cfg.web.bind)) body.web.bind = lan ? '0.0.0.0' : '127.0.0.1';
        if (this.newScanner) {
            const s = this.newScanner;
            body.scanner = { ip: s.ip, port: s.port || 80, protocol: s.protocol, name: s.name || '',
                             model: s.model || s.display_name || '', path: s.path || '' };
        }
        return body;
    },

    async save() {
        const body = this.collect();
        if (!body.output.folder) { toast('Please choose a folder for your scans.', 'error'); return; }
        if (body.folder_watch.enabled) {
            const norm = p => p.replace(/[\\/]+$/, '').toLowerCase();
            if (!body.folder_watch.watch_folder) { toast('Please enter the folder to watch.', 'error'); return; }
            if (norm(body.folder_watch.watch_folder) === norm(body.output.folder)) {
                toast('The watched folder must be different from the folder where scans go.', 'error');
                return;
            }
        }
        $('btn-save').disabled = true;
        try {
            const data = await api.post('api/settings', body);
            if (!data.saved) { toast(data.error || 'Could not save', 'error', 8000); return; }
            toast('Settings saved', 'success');
            (data.warnings || []).forEach(w => toast(w, 'warning', 8000));
            $('restart-alert').classList.toggle('hidden', !data.restart_needed);
            this.newScanner = null;
            this.cfg = await api.get('api/config');
            this.secrets = await api.get('api/secrets');
            this.fill();
        } finally {
            $('btn-save').disabled = false;
        }
    },

    async testPaperless() {
        const url = $('paperless-url').value.trim();
        const result = $('paperless-test-result');
        if (!url) { result.innerHTML = '<div class="alert alert-error">Please enter the address</div>'; return; }
        result.innerHTML = '<div class="loading"><div class="spinner"></div><span>Testing...</span></div>';
        const data = await api.post('api/test-paperless', { url, token: $('paperless-token').value.trim() });
        result.innerHTML = data.ok
            ? '<div class="alert alert-success">Connected to Paperless-NGX</div>'
            : `<div class="alert alert-error">Could not connect: ${esc(data.error)}</div>`;
    },

    revealToken() {
        this.tokenShown = !this.tokenShown;
        $('token-value').textContent = this.tokenShown ? this.secrets.api_token : '•'.repeat(12);
        $('btn-reveal').textContent = this.tokenShown ? 'Hide' : 'Show';
    },

    copyToken() { copyText(this.secrets.api_token); },

    async regenerateToken() {
        if (!confirm('Make a new access token? Other devices and Home Assistant will need the new one.')) return;
        const data = await api.post('api/token/regenerate', {});
        if (data.api_token) {
            this.secrets.api_token = data.api_token;
            this.tokenShown = false;
            this.revealToken();
            toast('New access token made', 'success');
        } else {
            toast(data.error || 'Could not make a new token', 'error');
        }
    },

    async openLogs() {
        const data = await api.post('api/open-log-folder', {});
        if (!data.opened) toast(`Open this folder yourself: ${data.folder}`, 'info', 8000);
    },

    async refreshProblems() {
        const [e, q] = await Promise.all([api.get('api/errors?limit=20'), api.get('api/queue')]);
        const errors = e.errors || [];
        $('errors-list').innerHTML = errors.length
            ? errors.map(x => `<li><span class="when">${esc(formatTime(x.time))}</span>${esc(x.message)}</li>`).join('')
            : '<li>Nothing to report.</li>';
        const pending = q.pending || [];
        $('queue-list').innerHTML = pending.length
            ? pending.map(x => `<li><span class="mono">${esc(String(x.file || '').split(/[\\/]/).pop())}</span>
                to ${esc(x.destination)} <button class="btn btn-secondary btn-small" data-id="${esc(x.id)}">Retry</button>
                ${x.last_error ? `<br><span class="when">${esc(x.last_error)}</span>` : ''}</li>`).join('')
            : '<li>No scans are waiting.</li>';
        $('queue-list').querySelectorAll('button[data-id]').forEach(b => {
            b.onclick = () => this.retry(b.dataset.id);
        });
        $('btn-retry-all').classList.toggle('hidden', pending.length === 0);
    },

    async retry(id) {
        const data = await api.post('api/queue/retry', id ? { id } : {});
        toast(data._status === 200 ? 'Trying again' : (data.error || 'Could not retry'), data._status === 200 ? 'info' : 'error');
        setTimeout(() => this.refreshProblems(), 1500);
    },
};

document.addEventListener('DOMContentLoaded', () => settings.init());
