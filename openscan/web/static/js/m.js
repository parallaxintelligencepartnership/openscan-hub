/* OpenScanHub - phone app. All URLs are relative so Home Assistant ingress works. */

(() => {
    const $ = (id) => document.getElementById(id);
    const SESSION_KEY = 'openscanhub.session';

    const state = {
        role: null,
        profiles: [],
        defaultId: '',
        sid: null,
        profileId: '',
        profileName: '',
        pages: [],
        scanning: false,
        statusTimer: null,
    };
    // Edits to one session run one after the other.
    let chain = Promise.resolve();

    // --- network ----------------------------------------------------------

    async function call(method, path, body, extra) {
        const opts = { method, credentials: 'same-origin', headers: {} };
        if (extra) {
            Object.assign(opts, extra);
        } else if (method === 'POST') {
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
        ['login', 'home', 'session'].forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
        if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
        if (view === 'home') {
            refreshStatus();
            state.statusTimer = setInterval(refreshStatus, 5000);
        }
    }

    function notice(text, kind) {
        const el = $('notice');
        if (!text) { el.classList.add('hidden'); return; }
        el.textContent = text;
        el.className = 'notice' + (kind ? ' ' + kind : '');
        el.scrollIntoView({ block: 'nearest' });
    }

    function busy(text) {
        $('busy-text').textContent = text || 'Working';
        $('busy').classList.toggle('hidden', !text);
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

    // --- home -------------------------------------------------------------

    async function refreshStatus() {
        const res = await call('GET', 'api/status');
        const dot = $('status-dot');
        if (res.status === 401) { boot(); return; }
        if (res.status !== 200) {
            dot.className = 'dot offline';
            $('status-name').textContent = 'OpenScanHub is not responding';
            $('status-detail').textContent = '';
            return;
        }
        const d = res.data;
        if (d.mode !== 'scanner') {
            dot.className = 'dot';
            $('status-name').textContent = d.mode === 'none' ? 'No scanner set up' : 'Scans arrive by ' + d.mode;
            $('status-detail').textContent = 'You can still add photos with the camera.';
            setScanEnabled(false);
            return;
        }
        const s = d.scanner || {};
        const st = d.status || {};
        $('status-name').textContent = s.display_name || s.name || 'Scanner';
        if (d.scan_in_progress || st.state === 'Processing') {
            dot.className = 'dot busy';
            $('status-detail').textContent = 'Busy';
        } else if (st.state === 'Offline') {
            dot.className = 'dot offline';
            $('status-detail').textContent = 'Not reachable. Check that the scanner is on.';
        } else {
            dot.className = 'dot online';
            $('status-detail').textContent = st.adf_state === 'ScannerAdfLoaded' ? 'Ready. Paper in feeder.' : 'Ready';
        }
        setScanEnabled(true);
    }

    function setScanEnabled(on) {
        document.querySelectorAll('.profile-btn').forEach((b) => { b.disabled = !on || state.scanning; });
        $('btn-scan-pages').disabled = !on;
    }

    async function loadProfiles() {
        const res = await call('GET', 'api/profiles');
        if (res.status !== 200) return;
        state.profiles = res.data.profiles || [];
        state.defaultId = res.data.default_profile_id || '';
        const box = $('profile-buttons');
        box.textContent = '';
        state.profiles.filter((p) => p.show_on_home).forEach((p) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn primary profile-btn';
            b.dataset.id = p.id;
            if (p.icon) {
                const i = document.createElement('span');
                i.className = 'icon';
                i.textContent = p.icon;
                b.appendChild(i);
            }
            b.appendChild(document.createTextNode(p.name));
            b.addEventListener('click', () => scanWithProfile(p));
            box.appendChild(b);
        });
        const sel = $('profile-select');
        sel.textContent = '';
        state.profiles.forEach((p) => {
            const o = document.createElement('option');
            o.value = p.id;
            o.textContent = p.name;
            sel.appendChild(o);
        });
        sel.value = state.defaultId || (state.profiles[0] && state.profiles[0].id) || '';
    }

    async function scanWithProfile(p) {
        state.scanning = true;
        setScanEnabled(false);
        notice('');
        busy('Scanning ' + p.name);
        const res = await call('POST', 'api/profiles/' + encodeURIComponent(p.id) + '/scan', {});
        busy('');
        state.scanning = false;
        if (res.status === 200 && res.data.success) {
            notice(res.data.message || 'Saved', 'ok');
        } else if (res.status === 200) {
            notice(res.data.message || 'The scan was not saved', 'error');
        } else {
            notice(errorText(res, 'The scan failed'), 'error');
        }
        refreshStatus();
    }

    // --- one session of pages ----------------------------------------------

    function selectedProfile() {
        const id = $('profile-select').value;
        return state.profiles.find((p) => p.id === id) || null;
    }

    async function startSession(uploadOnly) {
        const p = selectedProfile();
        const body = { profile_id: p ? p.id : undefined };
        if (uploadOnly) body.upload_only = true;
        const res = await call('POST', 'api/multipage/start', body);
        if (res.status !== 200 || !res.data.session_id) {
            notice(errorText(res, 'Could not start'), 'error');
            return false;
        }
        state.sid = res.data.session_id;
        state.profileId = p ? p.id : '';
        state.profileName = p ? p.name : '';
        state.pages = [];
        sessionStorage.setItem(SESSION_KEY, state.sid);
        return true;
    }

    function setInfo(info) {
        state.pages = info.pages || [];
        renderPages();
    }

    async function refreshInfo() {
        const res = await call('GET', 'api/multipage/status/' + encodeURIComponent(state.sid));
        if (res.status === 200) { setInfo(res.data); return true; }
        return false;
    }

    function openSession() {
        notice('');
        $('session-title').textContent = state.profileName ? 'Pages for ' + state.profileName : 'Pages';
        renderPages();
        show('session');
    }

    function leaveSession() {
        state.sid = null;
        state.pages = [];
        sessionStorage.removeItem(SESSION_KEY);
        show('home');
    }

    function thumbUrl(i) {
        return 'api/multipage/thumbnail/' + encodeURIComponent(state.sid) + '/' + i + '?v=' + encodeURIComponent(state.pages[i]);
    }

    function toolButton(label, text, cls, onclick, disabled) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'tool ' + (cls || '');
        b.setAttribute('aria-label', label);
        b.title = label;
        b.textContent = text;
        b.disabled = !!disabled;
        if (onclick) b.addEventListener('click', onclick);
        return b;
    }

    function renderPages() {
        const list = $('page-list');
        list.textContent = '';
        const n = state.pages.length;
        state.pages.forEach((id, i) => {
            const li = document.createElement('li');
            li.className = 'page';
            li.dataset.i = String(i);
            const thumb = document.createElement('div');
            thumb.className = 'thumb';
            const img = document.createElement('img');
            img.alt = 'Page ' + (i + 1);
            img.src = thumbUrl(i);
            thumb.appendChild(img);
            const meta = document.createElement('div');
            meta.className = 'meta';
            const num = document.createElement('div');
            num.className = 'num';
            num.textContent = 'Page ' + (i + 1);
            const tools = document.createElement('div');
            tools.className = 'tools';
            const drag = toolButton('Drag page ' + (i + 1) + ' to move it', '☰', 'drag', null, n < 2);
            drag.addEventListener('pointerdown', (ev) => startDrag(ev, li));
            tools.append(
                drag,
                toolButton('Move page ' + (i + 1) + ' up', '↑', 'up', () => move(i, i - 1), i === 0),
                toolButton('Move page ' + (i + 1) + ' down', '↓', 'down', () => move(i, i + 1), i === n - 1),
                toolButton('Rotate page ' + (i + 1), '↻', 'rotate', () => rotate(i)),
                toolButton('Delete page ' + (i + 1), '✕', 'del', () => remove(i)),
            );
            meta.append(num, tools);
            li.append(thumb, meta);
            list.appendChild(li);
        });
        if (state.scanning) {
            const li = document.createElement('li');
            li.className = 'page placeholder';
            li.innerHTML = '<div class="thumb">Scanning</div><div class="meta"><div class="num"></div></div>';
            li.querySelector('.num').textContent = 'Page ' + (n + 1);
            list.appendChild(li);
        }
        $('session-count').textContent = n + (n === 1 ? ' page' : ' pages');
        $('page-empty').classList.toggle('hidden', n > 0 || state.scanning);
        $('btn-save').disabled = n === 0 || state.scanning;
        $('btn-scan-next').disabled = state.scanning;
    }

    function queue(fn) {
        chain = chain.then(fn, fn);
        return chain;
    }

    async function edit(path, body) {
        const res = await call('POST', 'api/multipage/' + path, Object.assign({ session_id: state.sid }, body));
        if (res.status === 200) {
            setInfo(res.data);
        } else {
            notice(errorText(res, 'That change did not work'), 'error');
            await refreshInfo();
        }
    }

    function move(from, to) {
        if (to < 0 || to >= state.pages.length) return;
        const order = state.pages.map((_, i) => i);
        order.splice(to, 0, order.splice(from, 1)[0]);
        queue(() => edit('reorder', { order }));
    }

    function rotate(i) { queue(() => edit('rotate', { page_index: i, degrees: 90 })); }

    function remove(i) { queue(() => edit('delete-page', { page_index: i })); }

    // Drag with a finger or a mouse. The list reorders while you drag.
    function startDrag(ev, li) {
        if (ev.button > 0) return;
        ev.preventDefault();
        const list = $('page-list');
        const handle = ev.currentTarget;
        handle.setPointerCapture(ev.pointerId);
        li.classList.add('dragging');
        const onMove = (e) => {
            const others = Array.from(list.querySelectorAll('.page:not(.dragging):not(.placeholder)'));
            const next = others.find((o) => {
                const r = o.getBoundingClientRect();
                return e.clientY < r.top + r.height / 2;
            });
            if (next) list.insertBefore(li, next);
            else list.insertBefore(li, list.querySelector('.placeholder'));
        };
        const onEnd = () => {
            handle.removeEventListener('pointermove', onMove);
            handle.removeEventListener('pointerup', onEnd);
            handle.removeEventListener('pointercancel', onEnd);
            li.classList.remove('dragging');
            const order = Array.from(list.querySelectorAll('.page:not(.placeholder)')).map((o) => Number(o.dataset.i));
            if (order.some((v, k) => v !== k)) queue(() => edit('reorder', { order }));
            else renderPages();
        };
        handle.addEventListener('pointermove', onMove);
        handle.addEventListener('pointerup', onEnd);
        handle.addEventListener('pointercancel', onEnd);
    }

    async function scanNext() {
        if (state.scanning) return;
        state.scanning = true;
        notice('');
        renderPages();
        const res = await call('POST', 'api/multipage/scan', { session_id: state.sid });
        state.scanning = false;
        if (res.status !== 200 || !res.data.success) {
            notice(errorText(res, 'The scan failed'), 'error');
            await refreshInfo();
            renderPages();
            return;
        }
        await refreshInfo();
    }

    async function uploadFiles(files) {
        const list = Array.from(files);
        if (!list.length) return;
        busy('Adding ' + list.length + (list.length === 1 ? ' photo' : ' photos'));
        let failed = '';
        for (const f of list) {
            const res = await call('POST', 'api/multipage/upload?session_id=' + encodeURIComponent(state.sid), null, {
                body: f,
                headers: { 'Content-Type': f.type || 'application/octet-stream' },
            });
            if (res.status === 200) {
                setInfo(res.data);
            } else {
                failed = errorText(res, 'Could not add ' + f.name);
            }
        }
        busy('');
        notice(failed, failed ? 'error' : '');
    }

    async function save() {
        busy('Saving');
        const res = await call('POST', 'api/multipage/save', { session_id: state.sid, profile_id: state.profileId || undefined });
        busy('');
        if (res.status === 200 && (res.data.success || res.data.queued)) {
            leaveSession();
            notice(res.data.message || 'Saved', res.data.success ? 'ok' : '');
            return;
        }
        notice(errorText(res, 'Saving failed. Your pages are still here. Try again.'), 'error');
    }

    async function cancel() {
        if (state.pages.length && !window.confirm('Throw away these pages?')) return;
        await call('POST', 'api/multipage/cancel', { session_id: state.sid });
        leaveSession();
    }

    $('btn-scan-pages').addEventListener('click', async () => {
        if (await startSession(false)) openSession();
    });
    $('btn-scan-next').addEventListener('click', scanNext);
    $('btn-save').addEventListener('click', save);
    $('btn-cancel').addEventListener('click', cancel);
    $('camera-home').addEventListener('change', async (ev) => {
        const files = Array.from(ev.target.files);
        ev.target.value = '';
        if (!files.length || !(await startSession(true))) return;
        openSession();
        await uploadFiles(files);
    });
    $('camera-session').addEventListener('change', async (ev) => {
        const files = Array.from(ev.target.files);
        ev.target.value = '';
        await uploadFiles(files);
    });

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
        const saved = sessionStorage.getItem(SESSION_KEY);
        if (saved) {
            state.sid = saved;
            if (await refreshInfo()) {
                const info = await call('GET', 'api/multipage/status/' + encodeURIComponent(saved));
                const p = state.profiles.find((x) => x.id === info.data.profile_id);
                state.profileId = p ? p.id : '';
                state.profileName = p ? p.name : '';
                openSession();
                return;
            }
            sessionStorage.removeItem(SESSION_KEY);
            state.sid = null;
        }
        show('home');
    }

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('sw.js').catch(() => { /* needs https or localhost */ });
        });
    }

    boot();
})();
