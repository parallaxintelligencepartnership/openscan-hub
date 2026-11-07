/* OpenScanHub - scan profiles: the editor page and the dashboard buttons. */

const PROC_TOGGLES = ['ocr', 'remove_blank_pages', 'deskew', 'auto_rotate', 'crop', 'pdfa'];
const NAME_FIELDS = ['date', 'time', 'n', 'profile', 'source', 'doc_date', 'title', 'vendor', 'pages', 'barcode'];

async function runProfileScan(id) {
    const overlay = document.getElementById('scan-overlay');
    if (overlay) overlay.classList.add('active');
    try {
        const data = await api.post(`api/profiles/${encodeURIComponent(id)}/scan`, {});
        scanResultToast(data);
        return data;
    } catch (err) {
        toast(`Error: ${err.message}`, 'error');
        return null;
    } finally {
        if (overlay) overlay.classList.remove('active');
    }
}

/* --- dashboard: one button per profile ----------------------------------- */

const profileButtons = {
    feedMode: false,   // the feeder monitor is on: a button picks the next feed
    nextId: '',
    timer: null,

    async init() {
        const box = document.getElementById('profile-buttons');
        if (!box) return;
        try {
            const [status, data, edition] = await Promise.all(
                [api.get('api/status'), api.get('api/profiles'), api.get('api/edition')]);
            // The scan-only role may scan with a profile but not edit one.
            const link = document.getElementById('profiles-link');
            if (link) link.classList.toggle('hidden', edition.role === 'scan');
            const admin = edition.role !== 'scan';
            const shown = status.mode === 'scanner' ? (data.profiles || []).filter(p => p.show_on_home) : [];
            if (!admin && shown.length === 0) return;
            this.feedMode = status.mode === 'scanner' && !!status.monitor_enabled;
            document.getElementById('profile-button-list').innerHTML = shown.map(p => `
                <button class="btn btn-primary profile-button" data-id="${esc(p.id)}">
                    <span class="profile-icon">${esc(p.icon || '\u{1F4C4}')}</span>
                    <span>${esc(p.name)}</span>
                </button>`).join('') || '<p class="hint">Make a profile for each kind of document you scan.</p>';
            const title = box.querySelector('h2');
            if (this.feedMode) {
                if (title) title.textContent = 'Next feed profile';
                const list = document.getElementById('profile-button-list');
                const hint = document.createElement('p');
                hint.className = 'hint';
                hint.id = 'next-feed-hint';
                list.parentNode.insertBefore(hint, list);
            }
            box.querySelectorAll('.profile-button').forEach(btn => {
                btn.addEventListener('click', async () => {
                    if (this.feedMode) {
                        await this.choose(btn.dataset.id);
                        return;
                    }
                    await runProfileScan(btn.dataset.id);
                    if (window.dashboard) { dashboard.refreshHistory(); dashboard.refreshProblems(); }
                });
            });
            box.classList.remove('hidden');
            if (this.feedMode) {
                await this.refresh();
                this.timer = setInterval(() => { if (!document.hidden) this.refresh(); }, 5000);
            }
        } catch (err) {
            console.error('Profile buttons failed:', err);
        }
    },

    // Tap a profile to use it for the next batch from the feeder. Tap it again to undo.
    async choose(id) {
        try {
            const data = await api.post('api/feed/next', { profile_id: id === this.nextId ? null : id });
            this.show(data);
            const p = data.next_profile;
            toast(p ? `Next feed: ${p.name}. Load the paper.` : 'Next feed cleared', 'success');
        } catch (err) {
            toast(`Error: ${err.message}`, 'error');
        }
    },

    async refresh() {
        try {
            this.show(await api.get('api/feed'));
        } catch (err) {
            console.error('Next feed failed:', err);
        }
    },

    show(data) {
        const next = data.next_profile;
        this.nextId = next ? next.id : '';
        document.querySelectorAll('#profile-buttons .profile-button').forEach(btn => {
            btn.setAttribute('aria-pressed', String(btn.dataset.id === this.nextId));
            btn.classList.toggle('active', btn.dataset.id === this.nextId);
        });
        const hint = document.getElementById('next-feed-hint');
        if (hint) {
            hint.textContent = next
                ? `Next feed: ${next.name}. Load the paper. Tap it again to cancel.`
                : 'Pick a profile, then load the paper in the feeder.';
        }
    },
};

/* --- profile editor page -------------------------------------------------- */

const profileEditor = {
    profiles: [],
    defaultId: '',
    types: [],
    current: null,      // the profile being edited (null = a new one)
    dests: [],          // [{type, options}] in the form
    previewTimer: null,

    async init() {
        if (!document.getElementById('profile-form')) return;
        const [d, v] = await Promise.all([api.get('api/destinations'), api.get('api/naming/vendors')]);
        this.types = d.destinations || [];
        document.getElementById('dest-add-type').innerHTML =
            this.types.map(t => `<option value="${esc(t.type)}">${esc(t.label)}</option>`).join('');
        document.getElementById('vendors').value = (v.vendors || []).join('\n');
        document.getElementById('field-chips').innerHTML = NAME_FIELDS.map(f =>
            `<button type="button" class="chip" data-field="${f}">{${f}}</button>`).join('');
        document.querySelectorAll('#field-chips .chip').forEach(chip =>
            chip.addEventListener('click', () => this.insertField(chip.dataset.field)));
        for (const id of ['p-name-rule', 'p-sample', 'p-name']) {
            document.getElementById(id).addEventListener('input', () => this.schedulePreview());
        }
        await this.load();
        const wanted = new URLSearchParams(window.location.search).get('id');
        if (wanted && this.profiles.some(p => p.id === wanted)) this.edit(wanted);
    },

    async load() {
        const data = await api.get('api/profiles');
        this.profiles = data.profiles || [];
        this.defaultId = data.default_profile_id;
        this.renderList();
    },

    renderList() {
        const list = document.getElementById('profile-list');
        list.innerHTML = this.profiles.map((p, i) => `
            <li class="profile-item${this.current && this.current.id === p.id ? ' selected' : ''}" data-id="${esc(p.id)}">
                <span class="profile-icon">${esc(p.icon || '\u{1F4C4}')}</span>
                <span class="profile-name">${esc(p.name)}${p.id === this.defaultId ? ' <span class="badge badge-escl">DEFAULT</span>' : ''}${p.show_on_home ? '' : ' <span class="hint-inline">hidden on dashboard</span>'}</span>
                <span class="profile-actions">
                    <button type="button" class="btn btn-secondary btn-small" data-act="edit">Edit</button>
                    <button type="button" class="btn btn-secondary btn-small" data-act="up" ${i === 0 ? 'disabled' : ''} aria-label="Move up">&uarr;</button>
                    <button type="button" class="btn btn-secondary btn-small" data-act="down" ${i === this.profiles.length - 1 ? 'disabled' : ''} aria-label="Move down">&darr;</button>
                    <button type="button" class="btn btn-secondary btn-small" data-act="default" ${p.id === this.defaultId ? 'disabled' : ''}>Make default</button>
                    <button type="button" class="btn btn-danger btn-small" data-act="delete" ${this.profiles.length < 2 ? 'disabled' : ''}>Delete</button>
                </span>
            </li>`).join('');
        list.querySelectorAll('.profile-item').forEach(li => {
            li.querySelectorAll('button[data-act]').forEach(btn =>
                btn.addEventListener('click', () => this.action(btn.dataset.act, li.dataset.id)));
        });
    },

    async action(act, id) {
        if (act === 'edit') { this.edit(id); return; }
        let data;
        if (act === 'up' || act === 'down') {
            const ids = this.profiles.map(p => p.id);
            const i = ids.indexOf(id), j = act === 'up' ? i - 1 : i + 1;
            [ids[i], ids[j]] = [ids[j], ids[i]];
            data = await api.post('api/profiles/reorder', { ids });
        } else if (act === 'default') {
            data = await api.post(`api/profiles/${encodeURIComponent(id)}/default`, {});
        } else if (act === 'delete') {
            const p = this.profiles.find(x => x.id === id);
            if (!window.confirm(`Delete the profile "${p ? p.name : id}"?`)) return;
            data = await api.post(`api/profiles/${encodeURIComponent(id)}/delete`, {});
            if (data._status === 200 && this.current && this.current.id === id) this.close();
        }
        if (data._status !== 200) { toast(data.error || 'That did not work', 'error'); return; }
        this.profiles = data.profiles;
        this.defaultId = data.default_profile_id;
        this.renderList();
    },

    newProfile() {
        const base = this.profiles.find(p => p.id === this.defaultId) || this.profiles[0] || {};
        const copy = JSON.parse(JSON.stringify(base));
        copy.id = '';
        copy.name = '';
        copy.icon = '';
        copy.follow_settings = false;
        copy.show_on_home = true;
        for (const d of copy.destinations || []) {
            for (const k of Object.keys(d.options || {})) if (k.startsWith('has_')) delete d.options[k];
        }
        this.fill(null, copy);
    },

    edit(id) {
        const p = this.profiles.find(x => x.id === id);
        if (p) this.fill(p, p);
    },

    fill(current, p) {
        this.current = current;
        const $ = id => document.getElementById(id);
        $('form-title').textContent = current ? `Edit ${current.name}` : 'New profile';
        $('follow-note').classList.toggle('hidden', !p.follow_settings);
        $('p-name').value = p.name || '';
        $('p-icon').value = p.icon || '';
        $('p-show-on-home').checked = !!p.show_on_home;
        $('p-source').value = p.source || 'auto';
        $('p-color').value = p.color_mode || 'color';
        this.setSelect('p-resolution', String(p.resolution || 300));
        $('p-page-size').value = p.page_size || 'auto';
        $('p-duplex').checked = !!p.duplex;
        const proc = p.processing || {};
        for (const k of PROC_TOGGLES) $(`proc-${k}`).checked = !!proc[k];
        $('proc-ocr_languages').value = (proc.ocr_languages || ['eng']).join(', ');
        $('proc-compression').value = proc.compression || 'none';
        $('proc-output_color').value = proc.output_color || 'keep';
        $('p-split').value = p.split || 'none';
        $('p-name-rule').value = p.name_rule || 'scan_{date}_{time}_{n}';
        this.dests = JSON.parse(JSON.stringify(p.destinations || []));
        this.renderDestinations();
        $('btn-scan-profile').classList.toggle('hidden', !current);
        $('profile-form').classList.remove('hidden');
        this.renderList();
        this.preview();
        $('profile-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
    },

    setSelect(id, value) {
        const sel = document.getElementById(id);
        if (![...sel.options].some(o => o.value === value)) {
            sel.insertAdjacentHTML('beforeend', `<option value="${esc(value)}">${esc(value)} dpi</option>`);
        }
        sel.value = value;
    },

    close() {
        this.current = null;
        document.getElementById('profile-form').classList.add('hidden');
        this.renderList();
    },

    typeInfo(type) {
        return this.types.find(t => t.type === type);
    },

    renderDestinations() {
        const box = document.getElementById('dest-list');
        if (this.dests.length === 0) {
            box.innerHTML = '<p class="hint">No destination yet. Add one below.</p>';
            return;
        }
        box.innerHTML = this.dests.map((d, i) => {
            const info = this.typeInfo(d.type);
            if (!info) {
                return `<div class="dest-card" data-index="${i}"><p>${esc(d.type)} is not in this edition.</p>
                    <button type="button" class="btn btn-danger btn-small" data-act="remove">Remove</button></div>`;
            }
            const opts = d.options || {};
            const fields = info.fields.map(f => this.fieldHtml(i, f, opts)).join('');
            return `<div class="dest-card" data-index="${i}">
                <div class="dest-head"><strong>${esc(info.label)}</strong>
                    <span class="row">
                        <button type="button" class="btn btn-secondary btn-small" data-act="test">Test</button>
                        <button type="button" class="btn btn-danger btn-small" data-act="remove">Remove</button>
                    </span></div>
                ${fields}
                <div class="dest-test-result hint"></div>
            </div>`;
        }).join('');
        box.querySelectorAll('.dest-card').forEach(card => {
            const i = Number(card.dataset.index);
            card.querySelectorAll('button[data-act]').forEach(btn => btn.addEventListener('click', () => {
                if (btn.dataset.act === 'remove') { this.readDestinations(); this.dests.splice(i, 1); this.renderDestinations(); }
                else this.testDestination(i, card);
            }));
        });
    },

    fieldHtml(i, f, opts) {
        const id = `dest-${i}-${f.key}`;
        const value = opts[f.key] !== undefined ? opts[f.key] : f.default;
        const blankHint = '<small class="hint">Leave blank to use the value from Settings.</small>';
        if (f.kind === 'bool') {
            return `<label class="toggle"><input type="checkbox" id="${id}" ${value ? 'checked' : ''}>
                <span class="slider"></span><span class="label">${esc(f.label)}</span></label>`;
        }
        if (f.kind === 'choice') {
            return `<div class="form-group"><label for="${id}">${esc(f.label)}</label><select id="${id}">
                ${f.choices.map(c => `<option value="${esc(c)}" ${c === value ? 'selected' : ''}>${esc(c)}</option>`).join('')}
                </select></div>`;
        }
        if (f.kind === 'password') {
            const stored = opts[`has_${f.key}`];
            return `<div class="form-group"><label for="${id}">${esc(f.label)}</label>
                <input type="password" id="${id}" autocomplete="new-password" placeholder="${stored ? 'Saved. Leave blank to keep it.' : 'Leave blank to use the value from Settings'}">
                </div>`;
        }
        const text = Array.isArray(value) ? value.join(', ') : (value || '');
        return `<div class="form-group"><label for="${id}">${esc(f.label)}</label>
            <input type="text" id="${id}" value="${esc(text)}">
            ${f.kind === 'list' ? '<small class="hint">Separate with commas.</small>' : blankHint}</div>`;
    },

    readDestinations() {
        this.dests = this.dests.map((d, i) => {
            const info = this.typeInfo(d.type);
            if (!info) return d;
            const options = {};
            for (const f of info.fields) {
                const el = document.getElementById(`dest-${i}-${f.key}`);
                if (!el) continue;
                if (f.kind === 'bool') options[f.key] = el.checked;
                else if (f.kind === 'list') options[f.key] = el.value.split(',').map(s => s.trim()).filter(Boolean);
                else options[f.key] = el.value.trim();
                if (f.kind === 'password' && d.options && d.options[`has_${f.key}`] && !options[f.key]) {
                    options[`has_${f.key}`] = true;
                }
            }
            return { type: d.type, options };
        });
        return this.dests;
    },

    addDestination() {
        this.readDestinations();
        const type = document.getElementById('dest-add-type').value;
        if (!type) return;
        this.dests.push({ type, options: {} });
        this.renderDestinations();
    },

    async testDestination(i, card) {
        this.readDestinations();
        const d = this.dests[i];
        const out = card.querySelector('.dest-test-result');
        out.textContent = 'Testing...';
        const data = await api.post('api/destinations/test', {
            type: d.type, options: d.options, profile_id: this.current ? this.current.id : '', index: i,
        });
        out.textContent = data.message || data.error || (data.ok ? 'OK' : 'The test failed');
        out.className = `dest-test-result ${data.ok ? 'result-ok' : 'result-bad'}`;
    },

    insertField(field) {
        const input = document.getElementById('p-name-rule');
        const start = input.selectionStart ?? input.value.length;
        const end = input.selectionEnd ?? input.value.length;
        const token = `{${field}}`;
        input.value = input.value.slice(0, start) + token + input.value.slice(end);
        input.focus();
        input.setSelectionRange(start + token.length, start + token.length);
        this.schedulePreview();
    },

    schedulePreview() {
        clearTimeout(this.previewTimer);
        this.previewTimer = setTimeout(() => this.preview(), 250);
    },

    async preview() {
        const data = await api.post('api/profiles/preview', {
            name_rule: document.getElementById('p-name-rule').value,
            name: document.getElementById('p-name').value,
            text: document.getElementById('p-sample').value,
        });
        document.getElementById('name-preview').textContent = data.filename || data.error || '';
    },

    collect() {
        const $ = id => document.getElementById(id);
        const processing = { ocr_languages: $('proc-ocr_languages').value || 'eng',
                             compression: $('proc-compression').value,
                             output_color: $('proc-output_color').value };
        for (const k of PROC_TOGGLES) processing[k] = $(`proc-${k}`).checked;
        return {
            name: $('p-name').value.trim(),
            icon: $('p-icon').value.trim(),
            show_on_home: $('p-show-on-home').checked,
            source: $('p-source').value,
            color_mode: $('p-color').value,
            resolution: Number($('p-resolution').value),
            page_size: $('p-page-size').value,
            duplex: $('p-duplex').checked,
            processing,
            split: $('p-split').value,
            name_rule: $('p-name-rule').value.trim(),
            destinations: this.readDestinations(),
        };
    },

    async save() {
        const body = this.collect();
        const path = this.current ? `api/profiles/${encodeURIComponent(this.current.id)}` : 'api/profiles';
        const data = await api.post(path, body);
        if (data._status !== 200 && data._status !== 201) {
            toast(data.error || 'Could not save the profile', 'error', 8000);
            return false;
        }
        toast(`Saved ${data.profile.name}`, 'success', 2500);
        await this.load();
        this.edit(data.profile.id);
        return true;
    },

    async scanNow() {
        if (!this.current) return;
        await runProfileScan(this.current.id);
    },

    async saveVendors() {
        const vendors = document.getElementById('vendors').value.split('\n').map(s => s.trim()).filter(Boolean);
        const data = await api.post('api/naming/vendors', { vendors });
        if (data._status === 200) {
            document.getElementById('vendors').value = data.vendors.join('\n');
            toast('Saved vendor names', 'success', 2500);
            this.preview();
        } else {
            toast(data.error || 'Could not save vendor names', 'error');
        }
    },
};

document.addEventListener('DOMContentLoaded', () => {
    profileButtons.init();
    profileEditor.init();
});
