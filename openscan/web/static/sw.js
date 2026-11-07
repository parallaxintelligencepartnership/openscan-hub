/* OpenScanHub phone app service worker.
   It caches only the static shell: the page, its script and style, the manifest
   and the icons. It never touches /api/ requests, so scans, thumbnails and
   documents are never stored by the browser through this worker. */

const CACHE = 'openscanhub-shell-v1';
const SHELL = ['m', 'css/m.css', 'js/m.js', 'js/qr.js', 'manifest.webmanifest',
               'icons/icon-192.png', 'icons/icon-512.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE)
            .then((cache) => Promise.all(SHELL.map((url) =>
                fetch(url, { credentials: 'same-origin' })
                    .then((resp) => (resp.ok ? cache.put(new URL(url, self.location).href, resp) : null))
                    .catch(() => null))))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

function inShell(url) {
    const base = new URL('./', self.location).pathname;
    const path = url.pathname.startsWith(base) ? url.pathname.slice(base.length) : null;
    return path !== null && SHELL.includes(path);
}

self.addEventListener('fetch', (event) => {
    const req = event.request;
    const url = new URL(req.url);
    if (req.method !== 'GET' || url.origin !== self.location.origin || !inShell(url)) return;
    // Network first, so an update shows at once. The cache answers when offline.
    event.respondWith(
        fetch(req).then((resp) => {
            if (resp.ok) {
                const copy = resp.clone();
                caches.open(CACHE).then((cache) => cache.put(url.origin + url.pathname, copy));
            }
            return resp;
        }).catch(() => caches.match(url.origin + url.pathname))
    );
});
