/* OpenScanHub - Setup Wizard Logic */

const wizard = {
    currentStep: 1,
    totalSteps: 6,
    selectedScanner: null,
    isFolderWatch: false,

    nextStep() {
        if (this.currentStep >= this.totalSteps) return;

        // Validate current step
        if (!this.validateStep(this.currentStep)) return;

        this.currentStep++;
        this.showStep(this.currentStep);

        // Prepare content for the new step
        if (this.currentStep === 3) this.prepareTestStep();
        if (this.currentStep === 6) this.prepareSummary();
    },

    prevStep() {
        if (this.currentStep <= 1) return;
        this.currentStep--;
        this.showStep(this.currentStep);
    },

    showStep(step) {
        // Update step dots
        document.querySelectorAll('.step-dot').forEach(dot => {
            const s = parseInt(dot.dataset.step);
            dot.classList.remove('active', 'completed');
            if (s === step) dot.classList.add('active');
            else if (s < step) {
                dot.classList.add('completed');
                dot.textContent = '\u2713';
            } else {
                dot.textContent = s;
            }
        });

        // Show/hide step content
        document.querySelectorAll('.step-content').forEach(content => {
            content.classList.remove('active');
            if (parseInt(content.dataset.step) === step) {
                content.classList.add('active');
            }
        });
    },

    validateStep(step) {
        switch (step) {
            case 2:
                if (!this.selectedScanner && !this.isFolderWatch) {
                    return false;
                }
                return true;
            case 4:
                const folder = document.getElementById('output-folder').value.trim();
                if (!folder) {
                    document.getElementById('output-folder').style.borderColor = 'var(--error)';
                    return false;
                }
                document.getElementById('output-folder').style.borderColor = '';
                return true;
            default:
                return true;
        }
    },

    // Step 2: Discover scanners
    async discover() {
        const btn = document.getElementById('btn-discover');
        const loading = document.getElementById('discover-loading');
        const list = document.getElementById('scanner-list');
        const noScanners = document.getElementById('no-scanners');

        btn.classList.add('hidden');
        loading.classList.remove('hidden');
        list.innerHTML = '';
        noScanners.classList.add('hidden');

        try {
            const resp = await fetch('/api/discover?timeout=5');
            const data = await resp.json();

            loading.classList.add('hidden');
            btn.classList.remove('hidden');
            btn.textContent = 'Search Again';

            if (data.scanners && data.scanners.length > 0) {
                data.scanners.forEach(scanner => {
                    const li = document.createElement('li');
                    li.className = 'scanner-item';
                    li.onclick = () => this.selectScanner(scanner, li);
                    li.innerHTML = `
                        <div class="icon">\uD83D\uDDA8\uFE0F</div>
                        <div class="info">
                            <div class="name">${this.esc(scanner.display_name || scanner.name)}</div>
                            <div class="detail">${this.esc(scanner.ip)}:${scanner.port}</div>
                        </div>
                        <span class="badge badge-${scanner.protocol}">${scanner.protocol.toUpperCase()}</span>
                    `;
                    list.appendChild(li);
                });
            } else {
                noScanners.classList.remove('hidden');
            }
        } catch (err) {
            loading.classList.add('hidden');
            btn.classList.remove('hidden');
            list.innerHTML = `<div class="alert alert-error">Discovery failed: ${this.esc(err.message)}</div>`;
        }
    },

    selectScanner(scanner, element) {
        this.selectedScanner = scanner;
        this.isFolderWatch = false;
        document.querySelectorAll('.scanner-item').forEach(el => el.classList.remove('selected'));
        element.classList.add('selected');
        document.getElementById('btn-step2-next').disabled = false;
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
            const resp = await fetch('/api/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ip: ip, port: 80, protocol: 'escl' }),
            });
            const data = await resp.json();

            if (data.found) {
                this.selectedScanner = data.scanner;
                this.isFolderWatch = false;
                result.innerHTML = `<div class="alert alert-success">Found: ${this.esc(data.scanner.display_name || data.scanner.name)}</div>`;
                document.getElementById('btn-step2-next').disabled = false;
            } else {
                result.innerHTML = `<div class="alert alert-warning">${this.esc(data.error || 'No scanner found')}</div>`;
            }
        } catch (err) {
            result.innerHTML = `<div class="alert alert-error">Error: ${this.esc(err.message)}</div>`;
        }
    },

    selectFolderWatch() {
        const folder = document.getElementById('watch-folder').value.trim();
        if (!folder) {
            document.getElementById('watch-folder').style.borderColor = 'var(--error)';
            return;
        }
        document.getElementById('watch-folder').style.borderColor = '';

        this.isFolderWatch = true;
        this.selectedScanner = {
            name: 'Folder Watcher',
            ip: folder,
            port: 0,
            protocol: 'folder',
            model: 'Watch: ' + folder,
            display_name: 'Folder Watcher',
            sources: ['Platen'],
        };
        document.getElementById('btn-step2-next').disabled = false;

        // Clear scanner list selection
        document.querySelectorAll('.scanner-item').forEach(el => el.classList.remove('selected'));
    },

    // Step 3: Test connection
    prepareTestStep() {
        const info = document.getElementById('test-scanner-info');

        if (this.isFolderWatch) {
            info.innerHTML = `
                <div class="alert alert-info">Folder watcher mode - watching: ${this.esc(this.selectedScanner.ip)}</div>
                <p class="mt-8" style="font-size: 14px; color: var(--gray-500);">Folder watchers don't require a test scan. You can proceed to the next step.</p>
            `;
            document.getElementById('btn-test-scan').classList.add('hidden');
            return;
        }

        const s = this.selectedScanner;
        info.innerHTML = `
            <div style="padding: 12px; background: var(--gray-50); border-radius: 8px;">
                <div style="font-weight: 600; font-size: 16px;">${this.esc(s.display_name || s.name)}</div>
                <div style="font-size: 13px; color: var(--gray-500); margin-top: 4px;">
                    ${this.esc(s.ip)}:${s.port} &middot; ${s.protocol.toUpperCase()}
                </div>
                <div style="font-size: 13px; color: var(--gray-500); margin-top: 2px;">
                    Sources: ${(s.sources || ['Platen']).join(', ')}
                </div>
            </div>
        `;
        document.getElementById('btn-test-scan').classList.remove('hidden');
    },

    async testScan() {
        const btn = document.getElementById('btn-test-scan');
        const loading = document.getElementById('test-loading');
        const result = document.getElementById('test-result');

        btn.classList.add('hidden');
        loading.classList.remove('hidden');
        result.innerHTML = '';

        try {
            const resp = await fetch('/api/test-scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ip: this.selectedScanner.ip,
                    port: this.selectedScanner.port,
                    protocol: this.selectedScanner.protocol,
                    source: 'Platen',
                }),
            });
            const data = await resp.json();

            loading.classList.add('hidden');
            btn.classList.remove('hidden');

            if (data.success) {
                result.innerHTML = `<div class="alert alert-success">Test scan successful! (${this.esc(data.size_readable)})</div>`;
            } else {
                result.innerHTML = `<div class="alert alert-error">Test scan failed: ${this.esc(data.error)}</div>`;
            }
        } catch (err) {
            loading.classList.add('hidden');
            btn.classList.remove('hidden');
            result.innerHTML = `<div class="alert alert-error">Error: ${this.esc(err.message)}</div>`;
        }
    },

    // Step 5: Paperless
    togglePaperless() {
        const enabled = document.getElementById('paperless-enabled').checked;
        const config = document.getElementById('paperless-config');
        if (enabled) {
            config.classList.remove('hidden');
        } else {
            config.classList.add('hidden');
        }
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
            result.innerHTML = '<div class="alert alert-error">Please enter the Paperless URL</div>';
            return;
        }

        result.innerHTML = '<div class="loading"><div class="spinner"></div><span>Testing...</span></div>';

        try {
            const resp = await fetch('/api/test-paperless', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, token }),
            });
            const data = await resp.json();

            if (data.ok) {
                result.innerHTML = '<div class="alert alert-success">Connected to Paperless-NGX!</div>';
            } else {
                result.innerHTML = `<div class="alert alert-error">Connection failed: ${this.esc(data.error)}</div>`;
            }
        } catch (err) {
            result.innerHTML = `<div class="alert alert-error">Error: ${this.esc(err.message)}</div>`;
        }
    },

    // Step 6: Summary & Save
    prepareSummary() {
        const list = document.getElementById('config-summary');
        const s = this.selectedScanner || {};
        const outputFolder = document.getElementById('output-folder').value.trim();
        const paperlessEnabled = document.getElementById('paperless-enabled').checked;

        let items = [
            { label: 'Scanner', value: s.display_name || s.name || 'None' },
            { label: 'Protocol', value: (s.protocol || '').toUpperCase() || 'N/A' },
            { label: 'Address', value: s.ip ? `${s.ip}:${s.port}` : 'N/A' },
            { label: 'Output Folder', value: outputFolder || 'Not set' },
            { label: 'Paperless-NGX', value: paperlessEnabled ? 'Enabled' : 'Disabled' },
        ];

        if (paperlessEnabled) {
            const mode = document.getElementById('paperless-mode').value;
            items.push({ label: 'Paperless Mode', value: mode === 'api' ? 'API Upload' : 'Consume Folder' });
        }

        list.innerHTML = items.map(i => `
            <li>
                <span class="label">${this.esc(i.label)}</span>
                <span class="value">${this.esc(i.value)}</span>
            </li>
        `).join('');
    },

    async finish() {
        const s = this.selectedScanner || {};
        const paperlessEnabled = document.getElementById('paperless-enabled').checked;
        const paperlessMode = document.getElementById('paperless-mode').value;

        const config = {
            scanner: {
                ip: s.ip || '',
                port: s.port || 80,
                protocol: s.protocol || 'escl',
                name: s.name || '',
                model: s.model || s.display_name || '',
            },
            output: {
                folder: document.getElementById('output-folder').value.trim(),
                filename_pattern: document.getElementById('filename-pattern').value.trim() || 'scan_{date}_{time}_{n}',
            },
            paperless: {
                enabled: paperlessEnabled,
                mode: paperlessMode,
                consume_folder: document.getElementById('paperless-consume-folder').value.trim(),
                api_url: document.getElementById('paperless-url').value.trim(),
                api_token: document.getElementById('paperless-token').value.trim(),
            },
            monitor: {
                enabled: !this.isFolderWatch && (s.sources || []).includes('Feeder'),
            },
            folder_watch: {
                enabled: this.isFolderWatch,
                watch_folder: this.isFolderWatch ? s.ip : '',
            },
        };

        try {
            const resp = await fetch('/api/save-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config),
            });
            const data = await resp.json();

            if (data.saved) {
                window.location.href = '/dashboard';
            } else {
                alert('Failed to save configuration: ' + (data.error || 'Unknown error'));
            }
        } catch (err) {
            alert('Error saving configuration: ' + err.message);
        }
    },

    // Escape HTML to prevent XSS
    esc(str) {
        if (str == null) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    },
};
