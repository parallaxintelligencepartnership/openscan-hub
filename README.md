<p align="center">
  <h1 align="center">OpenScanHub</h1>
  <p align="center">
    <strong>Universal network scanner bridge</strong><br>
    Scan from any network scanner to your computer, NAS, Paperless-NGX, or Home Assistant.
  </p>
  <p align="center">
    <a href="https://github.com/parallaxintelligencepartnership/openscan-hub/releases"><img src="https://img.shields.io/github/v/release/parallaxintelligencepartnership/openscan-hub?style=flat-square" alt="Release"></a>
    <a href="https://github.com/parallaxintelligencepartnership/openscan-hub/blob/main/LICENSE"><img src="https://img.shields.io/github/license/parallaxintelligencepartnership/openscan-hub?style=flat-square" alt="License"></a>
    <a href="https://github.com/parallaxintelligencepartnership/openscan-hub"><img src="https://img.shields.io/github/stars/parallaxintelligencepartnership/openscan-hub?style=flat-square" alt="Stars"></a>
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  </p>
</p>

---

OpenScanHub connects your network scanner to wherever you want your documents to go. It supports multiple scanner protocols, auto-discovery, automatic document feeder monitoring, and a simple web-based setup wizard that **anyone** can use - no technical skills required.

Built by [Parallax Intelligence Partnership, LLC](https://parallaxintelligence.ai) and released as free, open-source software.

[parallaxintelligence.ai](https://parallaxintelligence.ai) | [parallaxintelligence.online](https://parallaxintelligence.online) | [GitHub](https://github.com/parallaxintelligencepartnership)

## Why OpenScanHub?

Most network scanners can scan - but getting those scans **where you actually need them** is the hard part. OpenScanHub solves this:

- **Your scanner** talks eSCL, WSD, or saves to a folder
- **OpenScanHub** catches the scan and routes it
- **Your documents** land in your folder, NAS, Paperless-NGX, or trigger a Home Assistant automation

No drivers to install. No vendor apps. One setup wizard and you're done.

## Features

- **Three scanner protocols** - eSCL (AirScan/AirPrint), WSD (Web Services for Devices), and folder watching
- **Auto-discovery** - Finds compatible scanners on your network automatically
- **Paperless-NGX** - Direct API upload or consume folder drop
- **Home Assistant** - REST commands for scan buttons on your HA dashboard
- **ADF auto-scan** - Automatically scans when paper is placed in the document feeder
- **Folder watcher** - Route files from any scanner that saves to a network share
- **Web UI** - Setup wizard + scan dashboard, works on phone and desktop
- **Three deployment options** - Windows EXE, Docker, or pip install

## Quick Start

### Option 1: Windows EXE (easiest)

1. Download **`OpenScanHub.exe`** from the [latest release](https://github.com/parallaxintelligencepartnership/openscan-hub/releases/latest)
2. Double-click to run
3. Your browser opens to the setup wizard
4. Follow the 6 steps to configure your scanner

The app runs in your system tray. Right-click for quick scan options.

### Option 2: Docker (Linux, NAS, servers)

```bash
git clone https://github.com/parallaxintelligencepartnership/openscan-hub.git
cd openscan-hub
docker compose up -d
```

Open `http://your-server:8020` in your browser to run the setup wizard.

> **Note:** `network_mode: host` is required for mDNS scanner auto-discovery. If you configure your scanner IP manually, you can use standard port mapping instead.

### Option 3: pip install (developers)

```bash
pip install openscan-hub
openscan
```

Open `http://localhost:8020` in your browser.

## Supported Scanners

| Protocol | Scanners | Discovery |
|----------|----------|-----------|
| **eSCL** (AirScan) | HP, Canon, Epson, Brother, and most printers made after ~2015 | Automatic via mDNS |
| **WSD** | Xerox, Ricoh, and enterprise MFPs | Automatic via WS-Discovery |
| **Folder Watch** | Any scanner that saves to a network share | Manual path configuration |

Don't see your scanner? If it's on your network and less than 10 years old, it almost certainly supports eSCL. Try the auto-discovery first.

## Home Assistant Integration

OpenScanHub exposes a simple REST API that makes it easy to add **Scan** buttons to your Home Assistant dashboard.

### Step 1: Add REST Commands

Add the following to your Home Assistant `configuration.yaml` (replace `OPENSCAN_IP` with the IP/hostname where OpenScanHub is running):

```yaml
rest_command:
  openscan_adf:
    url: "http://OPENSCAN_IP:8020/api/scan/adf"
    method: GET
    timeout: 120
  openscan_flatbed:
    url: "http://OPENSCAN_IP:8020/api/scan/flatbed"
    method: GET
    timeout: 120
  openscan_status:
    url: "http://OPENSCAN_IP:8020/api/status"
    method: GET
```

### Step 2: Restart Home Assistant

Go to **Developer Tools > YAML > Check Configuration**, then restart.

### Step 3: Add Dashboard Buttons

Add cards to your HA dashboard. Here's a simple example using the **Button** card:

```yaml
type: horizontal-stack
cards:
  - type: button
    name: Scan (Feeder)
    icon: mdi:file-document-arrow-right
    tap_action:
      action: perform-action
      perform_action: rest_command.openscan_adf
    hold_action:
      action: url
      url_path: http://OPENSCAN_IP:8020/dashboard
  - type: button
    name: Scan (Flatbed)
    icon: mdi:scanner
    tap_action:
      action: perform-action
      perform_action: rest_command.openscan_flatbed
    hold_action:
      action: url
      url_path: http://OPENSCAN_IP:8020/dashboard
```

> **Tip:** Long-press either button to open the full OpenScanHub dashboard in your browser.

### Step 4: Automations (Optional)

You can also trigger scans from HA automations:

```yaml
automation:
  - alias: "Morning Document Scan"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - action: rest_command.openscan_adf
```

## Paperless-NGX Integration

OpenScanHub supports two methods of sending scans to Paperless-NGX:

| Method | Best For | Setup |
|--------|----------|-------|
| **Consume Folder** | Paperless on the same machine or NAS | Point to your Paperless consume directory |
| **REST API** | Remote Paperless instances | Enter URL + API token in the wizard |

Both methods are configured during the setup wizard. You can also enable both for redundancy.

## Configuration

Config is stored at:

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\OpenScanHub\config.json` |
| Docker | `/config/openscan.json` |
| Linux/Mac | `~/.config/openscan/config.json` |

Override with the `OPENSCAN_CONFIG_DIR` environment variable.

## Command Line Options

```
openscan [options]

  --port PORT        Web UI port (default: 8020)
  --no-browser       Don't auto-open browser on startup
  --log-level LEVEL  DEBUG, INFO, WARNING, or ERROR
  --version          Show version and exit
```

## Building from Source

### Windows EXE

```bash
pip install -e ".[tray,dev]"
python build_exe.py
# Output: dist/OpenScanHub.exe
```

### Docker Image

```bash
docker build -t openscan-hub .
```

## Architecture

```
openscan/
  scanner/
    base.py          # Abstract scanner protocol interface
    escl.py          # eSCL (AirScan) implementation
    wsd.py           # WSD implementation
    folder_watch.py  # Folder watcher implementation
  web/
    server.py        # HTTP server + routing
    wizard_api.py    # Setup wizard API endpoints
    dashboard_api.py # Dashboard API endpoints
    static/          # HTML, CSS, JS (vanilla, no frameworks)
  config.py          # JSON config management
  discovery.py       # Unified scanner discovery
  output.py          # Output handlers (file save, Paperless)
  monitor.py         # ADF state monitor (auto-scan)
  main.py            # CLI entry point
  tray.py            # Windows system tray (pystray)
```

## Dependencies

| Package | License | Purpose | Required |
|---------|---------|---------|----------|
| [zeroconf](https://pypi.org/project/zeroconf/) | Apache 2.0 | mDNS scanner discovery | Yes |
| [pystray](https://pypi.org/project/pystray/) | LGPL v3 | Windows system tray icon | Windows EXE only |
| [Pillow](https://pypi.org/project/Pillow/) | HPND | Tray icon rendering | Windows EXE only |

Everything else is Python standard library. No JavaScript frameworks. No build tools. Just Python.

## Contributing

Contributions are welcome! Please open an issue first to discuss what you'd like to change.

## License

[MIT](LICENSE) - Copyright (c) 2025 [Parallax Intelligence Partnership, LLC](https://parallaxintelligence.ai)
