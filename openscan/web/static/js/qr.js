/* OpenScanHub - small QR code encoder. Written for this project; no outside code.
   Byte mode, error correction level L, versions 1 to 6 (up to 134 bytes).
   QR.encode(text) -> array of rows of booleans (true = dark), without quiet zone.
   QR.draw(canvas, text, cell) draws it black on white with a 4-cell quiet zone. */

const QR = (() => {
    const EXP = new Array(512);
    const LOG = new Array(256);
    let v = 1;
    for (let i = 0; i < 255; i++) {
        EXP[i] = v;
        LOG[v] = i;
        v <<= 1;
        if (v & 256) v ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
    const mul = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);

    function generator(n) {
        let g = [1];
        for (let i = 0; i < n; i++) {
            const next = new Array(g.length + 1).fill(0);
            for (let j = 0; j < g.length; j++) {
                next[j] ^= g[j];
                next[j + 1] ^= mul(g[j], EXP[i]);
            }
            g = next;
        }
        return g;
    }

    function remainder(data, n) {
        const g = generator(n);
        const res = new Array(n).fill(0);
        for (const d of data) {
            const f = d ^ res[0];
            res.shift();
            res.push(0);
            for (let i = 0; i < n; i++) res[i] ^= mul(g[i + 1], f);
        }
        return res;
    }

    // Level L: version -> [error codewords per block, blocks, data codewords per block]
    const LEVEL_L = { 1: [7, 1, 19], 2: [10, 1, 34], 3: [15, 1, 55], 4: [20, 1, 80], 5: [26, 1, 108], 6: [18, 2, 68] };
    const ALIGN = { 2: 18, 3: 22, 4: 26, 5: 30, 6: 34 }; // centre of the one alignment pattern

    function utf8(text) {
        return Array.from(new TextEncoder().encode(text));
    }

    function codewords(bytes, version) {
        const [ecLen, blocks, perBlock] = LEVEL_L[version];
        const total = blocks * perBlock;
        const bits = [];
        const push = (value, count) => { for (let i = count - 1; i >= 0; i--) bits.push((value >> i) & 1); };
        push(4, 4);
        push(bytes.length, 8);
        bytes.forEach((b) => push(b, 8));
        for (let i = 0; i < 4 && bits.length < total * 8; i++) bits.push(0);
        while (bits.length % 8) bits.push(0);
        const data = [];
        for (let i = 0; i < bits.length; i += 8) {
            let byte = 0;
            for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
            data.push(byte);
        }
        for (let pad = 0; data.length < total; pad++) data.push(pad % 2 ? 0x11 : 0xec);
        const dataBlocks = [];
        const ecBlocks = [];
        for (let b = 0; b < blocks; b++) {
            const chunk = data.slice(b * perBlock, (b + 1) * perBlock);
            dataBlocks.push(chunk);
            ecBlocks.push(remainder(chunk, ecLen));
        }
        const out = [];
        for (let i = 0; i < perBlock; i++) dataBlocks.forEach((blk) => out.push(blk[i]));
        for (let i = 0; i < ecLen; i++) ecBlocks.forEach((blk) => out.push(blk[i]));
        return out;
    }

    function baseMatrix(version) {
        const size = 17 + 4 * version;
        const m = Array.from({ length: size }, () => new Array(size).fill(null));
        const finder = (r0, c0) => {
            for (let r = -1; r <= 7; r++) {
                for (let c = -1; c <= 7; c++) {
                    const rr = r0 + r;
                    const cc = c0 + c;
                    if (rr < 0 || cc < 0 || rr >= size || cc >= size) continue;
                    const ring = Math.max(Math.abs(r - 3), Math.abs(c - 3));
                    m[rr][cc] = r >= 0 && r <= 6 && c >= 0 && c <= 6 && ring !== 2;
                }
            }
        };
        finder(0, 0);
        finder(0, size - 7);
        finder(size - 7, 0);
        for (let i = 8; i < size - 8; i++) {
            m[6][i] = i % 2 === 0;
            m[i][6] = i % 2 === 0;
        }
        if (ALIGN[version]) {
            const a = ALIGN[version];
            for (let r = -2; r <= 2; r++) {
                for (let c = -2; c <= 2; c++) {
                    m[a + r][a + c] = Math.max(Math.abs(r), Math.abs(c)) !== 1;
                }
            }
        }
        m[size - 8][8] = true; // the fixed dark module
        return m;
    }

    const MASKS = [
        (i, j) => (i + j) % 2 === 0,
        (i, j) => i % 2 === 0,
        (i, j) => j % 3 === 0,
        (i, j) => (i + j) % 3 === 0,
        (i, j) => (Math.floor(i / 2) + Math.floor(j / 3)) % 2 === 0,
        (i, j) => ((i * j) % 2) + ((i * j) % 3) === 0,
        (i, j) => (((i * j) % 2) + ((i * j) % 3)) % 2 === 0,
        (i, j) => (((i + j) % 2) + ((i * j) % 3)) % 2 === 0,
    ];

    function formatBits(mask) {
        const data = (1 << 3) | mask; // level L is 01
        let d = data << 10;
        const len = (n) => n.toString(2).length;
        while (len(d) >= 11) d ^= 0x537 << (len(d) - 11);
        return ((data << 10) | d) ^ 0x5412;
    }

    function place(base, codes, mask) {
        const size = base.length;
        const m = base.map((row) => row.slice());
        const fixed = base.map((row) => row.map((x) => x !== null));
        // Reserve the format areas so data skips them.
        for (let i = 0; i < 9; i++) { fixed[8][i] = true; fixed[i][8] = true; }
        for (let i = 0; i < 8; i++) { fixed[8][size - 1 - i] = true; fixed[size - 1 - i][8] = true; }
        let dir = -1;
        let row = size - 1;
        let bit = 7;
        let byte = 0;
        for (let col = size - 1; col > 0; col -= 2) {
            if (col === 6) col--;
            for (;;) {
                for (let c = 0; c < 2; c++) {
                    const cc = col - c;
                    if (fixed[row][cc]) continue;
                    let dark = byte < codes.length ? ((codes[byte] >> bit) & 1) === 1 : false;
                    if (MASKS[mask](row, cc)) dark = !dark;
                    m[row][cc] = dark;
                    if (--bit < 0) { byte++; bit = 7; }
                }
                row += dir;
                if (row < 0 || row >= size) { row -= dir; dir = -dir; break; }
            }
        }
        const fb = formatBits(mask);
        for (let i = 0; i < 15; i++) {
            const dark = ((fb >> i) & 1) === 1;
            if (i < 6) m[i][8] = dark;
            else if (i < 8) m[i + 1][8] = dark;
            else m[size - 15 + i][8] = dark;
            if (i < 8) m[8][size - 1 - i] = dark;
            else if (i < 9) m[8][15 - i] = dark;
            else m[8][14 - i] = dark;
        }
        m[size - 8][8] = true;
        return m;
    }

    function penalty(m) {
        const n = m.length;
        let score = 0;
        const runs = (get) => {
            for (let a = 0; a < n; a++) {
                let run = 1;
                for (let b = 1; b <= n; b++) {
                    if (b < n && get(a, b) === get(a, b - 1)) { run++; continue; }
                    if (run >= 5) score += run - 2;
                    run = 1;
                }
            }
        };
        runs((a, b) => m[a][b]);
        runs((a, b) => m[b][a]);
        for (let r = 0; r < n - 1; r++) {
            for (let c = 0; c < n - 1; c++) {
                const x = m[r][c];
                if (x === m[r][c + 1] && x === m[r + 1][c] && x === m[r + 1][c + 1]) score += 3;
            }
        }
        const pat = [true, false, true, true, true, false, true, false, false, false, false];
        const look = (get) => {
            for (let a = 0; a < n; a++) {
                for (let b = 0; b <= n - 11; b++) {
                    let fwd = true;
                    let back = true;
                    for (let k = 0; k < 11; k++) {
                        if (get(a, b + k) !== pat[k]) fwd = false;
                        if (get(a, b + k) !== pat[10 - k]) back = false;
                    }
                    if (fwd) score += 40;
                    if (back) score += 40;
                }
            }
        };
        look((a, b) => m[a][b]);
        look((a, b) => m[b][a]);
        let dark = 0;
        m.forEach((row) => row.forEach((x) => { if (x) dark++; }));
        score += Math.floor(Math.abs((dark * 100) / (n * n) - 50) / 5) * 10;
        return score;
    }

    function encode(text) {
        const bytes = utf8(text);
        let version = 0;
        for (let ver = 1; ver <= 6; ver++) {
            const [ecLen, blocks, perBlock] = LEVEL_L[ver];
            if (bytes.length <= blocks * perBlock - 2) { version = ver; break; }
        }
        if (!version) throw new Error('Text is too long for the QR code');
        const codes = codewords(bytes, version);
        const base = baseMatrix(version);
        let best = null;
        let bestScore = Infinity;
        for (let mask = 0; mask < 8; mask++) {
            const m = place(base, codes, mask);
            const s = penalty(m);
            if (s < bestScore) { best = m; bestScore = s; }
        }
        return best;
    }

    function draw(canvas, text, cell) {
        const m = encode(text);
        const px = cell || 6;
        const quiet = 4;
        const size = (m.length + quiet * 2) * px;
        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, size, size);
        ctx.fillStyle = '#000000';
        m.forEach((row, r) => row.forEach((dark, c) => {
            if (dark) ctx.fillRect((c + quiet) * px, (r + quiet) * px, px, px);
        }));
    }

    return { encode, draw };
})();
