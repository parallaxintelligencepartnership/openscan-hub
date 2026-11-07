/* OpenScanHub - phone app. All URLs are relative so Home Assistant ingress works.
   Paper goes in the document feeder and the feeder monitor scans it. This page
   only picks the profile for the next batch and shows what happened. */

(() => {
    const $ = (id) => document.getElementById(id);
    const REFRESH_MS = 3000;

    const state = {
        role: null,
        profiles: [],
        nextId: '',
        deadline: 0,      // Date.now() value when the next-feed choice ends
        monitorOn: true,
        scannerOk: true,
        busy: false,
        refreshTimer: null,
        countTimer: null,
    };

    // --- network ----------------------------------------------------------

    async function call(method, path, body) {
        const opts = { method, credentials: 'same-origin', headers: {} };
        if (method === 'POST') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body || {});
        }
        let resp;
        try {
            resp = await fetch(path, opts);
        } catch (e) {
            return { status: 0, data: { error: 'Cannot reach OpenScanHub. Check the Wi-Fi.' } };
        }
        let data = {};
        try { data = await resp.json(); } catch (e) { data = {}; }
        return { status: resp.status, data };
    }

    // --- small UI helpers -------------------------------------------------

    function show(view) {
        ['login', 'home'].forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
        stopTimers();
        if (view === 'home') startTimers();
    }

    function notice(text, kind) {
        const el = $('notice');
        if (!text) { el.classList.add('hidden'); return; }
        el.textContent = text;
        el.className = 'notice' + (kind ? ' ' + kind : '');
    }

    function errorText(res, fallback) {
        return (res.data && (res.data.error || res.data.message)) || fallback;
    }

    // --- sign in ----------------------------------------------------------

    async function signIn(token) {
        const res = await call('POST', 'api/login', { token });
        if (res.data && res.data.ok) return true;
        notice(errorText(res, 'That token is not right'), 'error');
        return false;
    }

    $('login-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const token = $('token').value.trim();
        if (token && await signIn(token)) {
            $('token').value = '';
            notice('');
            boot();
        }
    });

    // --- refresh ------------------------------------------------------------

    function startTimers() {
        stopTimers();
        refresh();
        state.refreshTimer = setInterval(refresh, REFRESH_MS);
        state.countTimer = setInterval(renderCountdown, 1000);
    }

    function stopTimers() {
        if (state.refreshTimer) clearInterval(state.refreshTimer);
        if (state.countTimer) clearInterval(state.countTimer);
        state.refreshTimer = state.countTimer = null;
    }

    // Refresh only while the page is visible.
    document.addEventListener('visibilitychange', () => {
        if (!$('view-home').classList.contains('hidden')) {
            if (document.hidden) stopTimers(); else startTimers();
        }
    });

    async function refresh() {
        const res = await call('GET', 'api/feed');
        if (res.status === 401) { boot(); return; }
        if (res.status !== 200) {
            $('status-dot').className = 'dot offline';
            $('status-name').textContent = 'OpenScanHub is not responding';
            $('status-detail').textContent = '';
            return;
        }
        render(res.data);
    }

    function render(d) {
        renderStatus(d);
        const next = d.next_profile;
        state.nextId = next ? next.id : '';
        state.deadline = next ? Date.now() + (d.expires_in_seconds || 0) * 1000 : 0;
        state.monitorOn = d.monitor_enabled !== false;
        $('next-feed').classList.toggle('hidden', !next);
        $('next-text').textContent = next ? 'Next feed: ' + next.name + '. Load the paper.' : '';
        $('monitor-off').classList.toggle('hidden', state.monitorOn);
        renderCountdown();
        markButtons();
        renderScans(d.last_scans || []);
    }

    function renderStatus(d) {
        const dot = $('status-dot');
        const sc = d.scanner_state;
        const adf = d.adf_state;
        let feeder = '';
        if (adf === 'ScannerAdfLoaded') feeder = 'Paper in the feeder.';
        else if (adf === 'ScannerAdfEmpty') feeder = 'Feeder is empty.';
        else if (adf === 'ScannerAdfJam') feeder = 'Paper jam in the feeder.';
        $('status-name').textContent = 'Scanner';
        state.scannerOk = sc !== 'Offline';
        if (sc === 'Offline') {
            dot.className = 'dot offline';
            $('status-detail').textContent = 'Not reachable. Check that the scanner is on.';
        } else if (sc === 'Processing') {
            dot.className = 'dot busy';
            $('status-detail').textContent = 'Scanning. ' + feeder;
        } else if (sc === 'Unknown' && adf === 'Unknown') {
            dot.className = 'dot';
            $('status-detail').textContent = 'This setup receives scans from the scanner.';
        } else {
            dot.className = 'dot online';
            $('status-detail').textContent = 'Ready. ' + feeder;
        }
    }

    function renderCountdown() {
        if (!state.nextId) { $('next-count').textContent = ''; return; }
        const left = Math.max(0, Math.round((state.deadline - Date.now()) / 1000));
        const m = Math.floor(left / 60);
        const s = String(left % 60).padStart(2, '0');
        $('next-count').textContent = 'Ends in ' + m + ':' + s;
        if (left === 0) refresh();
    }

    const RESULT_TEXT = { ok: 'Saved', queued: 'Will retry', failed: 'Failed' };

    function renderScans(list) {
        const ul = $('last-scans');
        ul.textContent = '';
        list.forEach((sc) => {
            const li = document.createElement('li');
            li.className = 'scan ' + (sc.result || '');
            const name = document.createElement('div');
            name.className = 'scan-name';
            name.textContent = sc.file || 'Scan';
            const meta = document.createElement('div');
            meta.className = 'hint';
            const when = sc.time ? new Date(sc.time) : null;
            const timeText = when && !isNaN(when) ? when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
            meta.textContent = [timeText, sc.profile, RESULT_TEXT[sc.result] || sc.result].filter(Boolean).join(' - ');
            li.append(name, meta);
            ul.appendChild(li);
        });
        $('scans-empty').classList.toggle('hidden', list.length > 0);
    }

    // --- profiles -----------------------------------------------------------

    async function loadProfiles() {
        const res = await call('GET', 'api/profiles');
        if (res.status !== 200) return;
        state.profiles = (res.data.profiles || []).filter((p) => p.show_on_home);
        const box = $('profile-buttons');
        box.textContent = '';
        state.profiles.forEach((p) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn profile-btn';
            b.dataset.id = p.id;
            if (p.icon) {
                const i = document.createElement('span');
                i.className = 'icon';
                i.textContent = p.icon;
                b.appendChild(i);
            }
            b.appendChild(document.createTextNode(p.name));
            b.addEventListener('click', () => choose(p));
            box.appendChild(b);
        });
        markButtons();
    }

    function markButtons() {
        document.querySelectorAll('.profile-btn').forEach((b) => {
            const on = b.dataset.id === state.nextId;
            b.classList.toggle('primary', on);
            b.classList.toggle('secondary', !on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.disabled = state.busy || !state.monitorOn;
        });
    }

    async function setNext(profileId) {
        state.busy = true;
        markButtons();
        notice('');
        const res = await call('POST', 'api/feed/next', { profile_id: profileId });
        state.busy = false;
        if (res.status === 200) {
            const d = res.data;
            state.nextId = d.next_profile ? d.next_profile.id : '';
            await refresh();
        } else {
            notice(errorText(res, 'That did not work'), 'error');
            markButtons();
        }
    }

    // Tap a profile to use it for the next batch. Tap it again to undo.
    function choose(p) {
        setNext(p.id === state.nextId ? null : p.id);
    }

    $('btn-cancel-feed').addEventListener('click', () => setNext(null));

    // --- start --------------------------------------------------------------

    async function boot() {
        const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
        if (hash.get('token')) {
            const ok = await signIn(hash.get('token'));
            history.replaceState(null, '', location.pathname + location.search);
            if (!ok) { show('login'); return; }
        }
        const res = await call('GET', 'api/edition');
        if (res.status === 401) { show('login'); return; }
        if (res.status !== 200) { notice(errorText(res, 'OpenScanHub is not responding'), 'error'); show('login'); return; }
        state.role = res.data.role;
        const note = $('role-note');
        note.textContent = state.role === 'scan' ? 'Scan only' : '';
        note.classList.toggle('hidden', state.role !== 'scan');
        await loadProfiles();
        show('home');
    }

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('sw.js').catch(() => { /* needs https or localhost */ });
        });
    }

    boot();
})();
