/* OpenScanHub - shared helpers. All URLs are relative so Home Assistant ingress works. */

const api = {
    async request(method, path, body) {
        const opts = { method, credentials: 'same-origin', headers: {} };
        if (method === 'POST') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body || {});
        }
        const resp = await fetch(path.replace(/^\/+/, ''), opts);
        if (resp.status === 401 && !path.includes('api/login')) {
            window.location.href = 'login';
            throw new Error('Please sign in');
        }
        let data = {};
        try { data = await resp.json(); } catch (e) { data = {}; }
        data._status = resp.status;
        return data;
    },
    get(path) { return this.request('GET', path); },
    post(path, body) { return this.request('POST', path, body); },
};

function esc(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

function toast(message, type = 'info', ms = 5000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(() => el.remove(), 300);
    }, ms);
}

function formatSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        toast('Copied', 'success', 2000);
    } catch (e) {
        window.prompt('Copy this:', text);
    }
}

/* Message for a scan or save result from the API. Never green unless delivered. */
function scanResultToast(data) {
    if (data._status === 409) {
        toast('The scanner is busy with another scan. Try again in a moment.', 'warning');
    } else if (data.success) {
        toast(`${data.message || 'Saved'} (${formatSize(data.size_bytes)})`, 'success');
    } else if (data.queued) {
        toast(data.message || 'Scanned, but not delivered yet. It will be retried automatically.', 'warning', 8000);
    } else {
        toast(data.message || `Scan failed: ${data.error || 'unknown problem'}`, 'error', 8000);
    }
}

// Show save warnings (for example a service that could not start) in a
// yellow box that stays until the next save.
function showWarnings(elementId, warnings) {
    const box = document.getElementById(elementId);
    if (!box) return;
    const list = Array.isArray(warnings) ? warnings : [];
    box.innerHTML = list.map(w => `<div>${esc(w)}</div>`).join('');
    box.classList.toggle('hidden', list.length === 0);
}

// "Scanner connects to ftp://192.168.1.20:2121" for the FTP receiver.
function ftpConnectsText(address, port) {
    if (!address) return "Scanner connects to this computer's IP address, port " + port + '.';
    return `Scanner connects to ftp://${address}:${port}`;
}
