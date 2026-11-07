/* OpenScanHub - dashboard block: link and QR code for the phone page.
   The plain code holds only the page address. The "Show phone code" button adds
   the scan token after a "#", which the browser never sends to the server.
   The admin access token never goes into a code. */

const phoneLink = {
    withToken: false,
    scanToken: '',

    pageUrl() {
        return new URL('m', location.href).href;
    },

    isLocalName() {
        return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(location.hostname);
    },

    draw() {
        const url = this.pageUrl();
        const text = this.withToken && this.scanToken ? url + '#token=' + this.scanToken : url;
        QR.draw(document.getElementById('phone-qr'), text, 5);
        // The token never shows on screen as text.
        document.getElementById('phone-url').textContent = url;
        document.getElementById('phone-code-btn').textContent = this.withToken ? 'Hide phone code' : 'Show phone code';
        document.getElementById('phone-code-warning').classList.toggle('hidden', !this.withToken);
    },

    async toggle() {
        const noToken = document.getElementById('phone-no-token');
        if (this.withToken) {
            this.withToken = false;
            this.scanToken = '';
            this.draw();
            return;
        }
        const res = await api.get('api/secrets');
        this.scanToken = res.scan_token || '';
        noToken.classList.toggle('hidden', !!this.scanToken);
        this.withToken = !!this.scanToken;
        this.draw();
    },

    async init() {
        const info = await loadEdition();
        if (info.role === 'scan') return; // the code is for an admin to share
        document.getElementById('phone-card').classList.remove('hidden');
        document.getElementById('phone-local-note').classList.toggle('hidden', !this.isLocalName());
        document.getElementById('phone-code-btn').addEventListener('click', () => this.toggle());
        this.draw();
    },
};

document.addEventListener('DOMContentLoaded', () => phoneLink.init());
