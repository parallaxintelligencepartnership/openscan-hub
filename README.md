<p align="center">
  <h1 align="center">OpenScanHub</h1>
  <p align="center">
    <strong>Put paper on the scanner. It lands in your folder.</strong>
  </p>
  <p align="center">
    <a href="https://github.com/parallaxintelligencepartnership/openscan-hub/releases"><img src="https://img.shields.io/github/v/release/parallaxintelligencepartnership/openscan-hub?style=flat-square" alt="Release"></a>
    <a href="https://github.com/parallaxintelligencepartnership/openscan-hub/blob/main/LICENSE"><img src="https://img.shields.io/github/license/parallaxintelligencepartnership/openscan-hub?style=flat-square" alt="License"></a>
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  </p>
</p>

---

OpenScanHub connects the network scanner you already own to a folder on your
computer or NAS, with no drivers and no vendor apps. Paperless-NGX is an
optional extra destination.

Built by [Parallax Intelligence Partnership, LLC](https://parallaxintelligence.ai) and released as free, open-source software.

## Install

Pick one of three ways.

### 1. Windows app (easiest)

1. Download **`OpenScanHub.exe`** and **`SHA256SUMS.txt`** from the
   [latest release](https://github.com/parallaxintelligencepartnership/openscan-hub/releases/latest).
2. Double-click `OpenScanHub.exe`. Your browser opens the setup page.

The app is not code-signed yet, so Windows SmartScreen may show
**"Windows protected your PC"**. Click **More info**, then **Run anyway**.
If you want to check the file first, open PowerShell in your Downloads
folder and run:

```powershell
Get-FileHash .\OpenScanHub.exe -Algorithm SHA256
```

The long number it prints must match the line for `OpenScanHub.exe` in
`SHA256SUMS.txt`. If it does not, delete the file and download it again.
(With the GitHub CLI you can also check how it was built:
`gh attestation verify OpenScanHub.exe --repo parallaxintelligencepartnership/openscan-hub`.)

OpenScanHub then runs in the system tray. Right-click the tray icon to scan,
open the dashboard, or show your access token.

### 2. Docker (Linux, NAS, Raspberry Pi)

Images are published for **linux/amd64** and **linux/arm64**:

| Tag | Meaning |
|---|---|
| `ghcr.io/parallaxintelligencepartnership/openscan-hub:latest` | Newest stable release |
| `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.3` | Newest 0.3.x release |
| `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.3.0` | Exactly this version |

```bash
mkdir openscan-hub && cd openscan-hub
curl -O https://raw.githubusercontent.com/parallaxintelligencepartnership/openscan-hub/main/docker-compose.yml
mkdir -p scans
docker compose up -d
```

Open `http://<your-server>:8020`. Scans are saved to the `scans` folder next
to `docker-compose.yml` (inside the container it is `/scans`). Settings live
in the `openscan-config` volume (`/config`). The container runs as user ID
1000, so create the `scans` folder yourself before the first start, as shown.

Set `TZ` in `docker-compose.yml` to your time zone so file names carry your
local time. To find scanners automatically, switch to `network_mode: host`
(see the comments in the file); otherwise enter the scanner's IP address.

### 3. Python (any OS, for developers)

OpenScanHub is not on PyPI. Install a release straight from GitHub
(Python 3.10 or newer):

```bash
pip install "openscan-hub @ git+https://github.com/parallaxintelligencepartnership/openscan-hub@v0.3.0"
openscan
```

For the exact dependency versions used by the release builds:

```bash
git clone https://github.com/parallaxintelligencepartnership/openscan-hub.git
cd openscan-hub
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
openscan
```

Open `http://localhost:8020`.

## First run: three steps

1. **Find your scanner.** OpenScanHub looks for scanners on your network.
   If yours does not show up, type its IP address. If your scanner sends files
   by itself (Scan to FTP or Scan to folder), choose that option instead.
2. **Where should scans go?** A folder is already filled in (Windows:
   `Documents\Scans`, Mac and Linux: `~/Scans`, Docker: `/scans`). Change it
   if you like, for example to a NAS share. Paperless-NGX is an optional
   extra here.
3. **All set.** Scan a test page and check that it arrived.

You can change everything later on the Settings page (gear icon on the
dashboard).

## Where scans go

**A folder (the default).** Any folder works, including a NAS share such as
`\\nas\scans` on Windows or a mounted share like `/mnt/nas/scans` on Linux
and `/Volumes/scans` on a Mac. On its own this is a complete setup.

Scans are never thrown away. If the folder cannot be reached (the NAS is
asleep or offline), the dashboard says so, OpenScanHub keeps the scan, and it
tries again after 1 minute, 5 minutes, 15 minutes, 1 hour, and then once a
day. You can also press **Retry** on the dashboard. After 30 days it stops
trying but still keeps the file.

**Paperless-NGX (optional).** Turn it on in Settings if you use
[Paperless-NGX](https://docs.paperless-ngx.com/). Two ways to send:

| Way | Use it when | You enter |
|---|---|---|
| Consume folder | Paperless runs on the same machine or NAS | The Paperless consume folder |
| API upload | Paperless runs somewhere else | The Paperless address and an API token |

With API upload you can also set tags (created if missing), a correspondent
and a document type (these must already exist in Paperless). OpenScanHub
waits up to a minute for Paperless to confirm the document. "Keep a local
copy" is on by default; if you turn it off, the copy in your scans folder is
removed only after Paperless confirms it has the document.

## Other ways scans arrive

Most scanners are driven directly by OpenScanHub (eSCL/AirScan or WSD). Two
other ways cover the rest.

**FTP receiver** for scanners with "Scan to FTP". Turn it on in Settings,
then enter these on the scanner:

| Setting | Value |
|---|---|
| Server | The IP address of the computer running OpenScanHub |
| Port | `2121` |
| User name | `scan` |
| Password | Shown in Settings (a random password is created for you) |
| Folder | `/` |
| Format | PDF |

The FTP receiver only accepts uploads: nobody can download or delete files
through it, and anonymous login is off. Because the scanner has to reach it,
it listens on your whole network while it is turned on, and FTP itself is not
encrypted. Leave it off if you do not need it. With Docker, uncomment the FTP
ports in `docker-compose.yml`, or better, use `network_mode: host` so the
scanner can open the passive data ports (60000 to 60010).

**Folder watch** for scanners that save to a network folder by themselves.
OpenScanHub picks up new files from that folder and sends them to your scans
folder (and Paperless, if on). The watched folder must be a different folder:
it cannot be your scans folder, sit inside it, or contain it. Files already
in the watched folder when watching starts are left alone.

## Supported scanners

| Protocol | Scanners | How it works |
|---|---|---|
| **eSCL** (AirScan) | HP, Canon, Epson, Brother, and most printers made after about 2015 | OpenScanHub asks the scanner for pages. Found automatically. |
| **WSD** | Xerox, Ricoh, Canon and many office multifunction printers | OpenScanHub asks the scanner for pages. Found automatically. |
| **FTP receiver** | Any scanner with "Scan to FTP" | The scanner sends files to OpenScanHub. |
| **Folder watch** | Any scanner that saves to a network share | OpenScanHub picks up new files. |

## Access token and using it from other devices

Out of the box, OpenScanHub only answers on the computer it runs on. To use it
from a phone or another computer, turn on **Allow other devices on my network
to use OpenScanHub** in Settings and restart OpenScanHub. Other devices then
sign in once with the **access token**. You can see the token:

- in the tray menu: **Show access token** (Windows app),
- on the Settings page,
- by running `openscan --show-token`,
- in the log when OpenScanHub starts.

The Docker image listens on the network by default, so any device other than
the server itself needs the token (`docker logs openscan-hub` shows it at
startup, or run `docker exec openscan-hub openscan --show-token`). Settings has **Regenerate token** if it
ever leaks.

**Behind a reverse proxy.** A browser on the computer running OpenScanHub is
signed in automatically. A request that arrives through a reverse proxy
(nginx, Caddy, cloudflared, Tailscale serve) is treated as coming from another
device and needs the token, even when the proxy runs on the same computer:
any request with an `X-Forwarded-For`, `X-Real-IP`, `Forwarded` or
`X-Forwarded-Host` header counts as remote. Caddy, cloudflared and Tailscale
serve add `X-Forwarded-For` by themselves. nginx does not: add
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` to the
`location` block. If you are not sure what your proxy sends, set
`OPENSCAN_DISABLE_LOCAL_AUTOLOGIN=1`, which turns automatic sign-in off
completely.

## Home Assistant (optional)

Home Assistant is an optional add-on, not needed for anything above.

- Scan buttons and a health sensor for an existing install:
  [docs/home-assistant.md](docs/home-assistant.md).
- Run OpenScanHub as a Home Assistant add-on (amd64 and aarch64):
  [homeassistant-addon/](homeassistant-addon/) and its
  [DOCS.md](homeassistant-addon/openscanhub/DOCS.md).

## Settings file

Settings are stored in `openscan.json`:

| System | Folder |
|---|---|
| Windows | `%APPDATA%\OpenScanHub` |
| Mac | `~/Library/Application Support/OpenScanHub` |
| Linux | `~/.config/openscan` (or `$XDG_CONFIG_HOME/openscan`) |
| Docker | `/config` |

Set `OPENSCAN_CONFIG_DIR` to use a different folder. `OPENSCAN_BIND`,
`OPENSCAN_PORT` and `OPENSCAN_TOKEN` override the listen address, port and
access token without changing the file. If other devices reach OpenScanHub by
a plain host name (not an IP address or a `.local` name), list it in
`OPENSCAN_ALLOWED_HOSTS`, for example `OPENSCAN_ALLOWED_HOSTS=nas,nas.lan`.

## Troubleshooting

**Logs.** OpenScanHub writes `openscan.log` in a `logs` folder inside the
settings folder above, for example `%APPDATA%\OpenScanHub\logs\openscan.log`
on Windows, `~/Library/Application Support/OpenScanHub/logs/openscan.log` on
a Mac, `~/.config/openscan/logs/openscan.log` on Linux, and
`/config/logs/openscan.log` in Docker (also `docker logs openscan-hub`). The
tray menu has **Open log folder**, and the Settings page shows recent
problems.

**Is it running?** Open `http://localhost:8020/api/health`. It answers
without a token and shows the version, whether the scanner is online and how
many scans are waiting to be retried.

**Common problems**

- *Scanner not found:* type its IP address (printed on the scanner's network
  settings page). In Docker, automatic discovery needs `network_mode: host`.
- *"Could not reach \\\\nas\\scans":* the NAS is off or the path is wrong.
  The scan is kept and retried; check the share from File Explorer or Finder.
- *Port 8020 already in use:* start with `openscan --port 8030`, or change
  the port in Settings.
- *Windows says "Windows protected your PC":* see the Windows install
  section above.

## Command line options

```
openscan [options]

  --port PORT        Web page port (default: 8020)
  --no-browser       Don't open the browser on startup
  --log-level LEVEL  DEBUG, INFO, WARNING, or ERROR
  --show-token       Print the access token and exit
  --version          Show version and exit
```

## Building from source

Windows EXE (on Windows):

```bash
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
python build_exe.py
# Output: dist/OpenScanHub.exe
```

Docker image:

```bash
docker build -t openscan-hub .
```

Tests: `python -m pytest tests/ -q`.

To update the pinned dependencies after changing `pyproject.toml`:

```bash
uv pip compile pyproject.toml --all-extras --generate-hashes --universal --python-version 3.10 -o requirements.lock
```

## Third-party software

OpenScanHub uses zeroconf, pyftpdlib, pypdf, pypdfium2 (PDFium), Pillow and,
in the Windows app, pystray. Their licenses (MIT, BSD, Apache-2.0, MIT-CMU,
and LGPL for zeroconf and pystray) and links to their source are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd
like to change.

## License

OpenScanHub's own code is released under the [MIT License](LICENSE),
Copyright (c) 2025 [Parallax Intelligence Partnership, LLC](https://parallaxintelligence.ai).
The third-party packages it includes keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
