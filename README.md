# OpenScanHub

**Universal network scanner bridge** - scan from any network scanner to your computer, NAS, or Paperless-NGX.

OpenScanHub connects your network scanner to wherever you want your documents to go. It supports multiple scanner protocols, auto-discovery, ADF monitoring, and a simple web-based setup wizard that anyone can use.

## Features

- **Three scanner protocols**: eSCL (AirScan/AirPrint), WSD (Web Services for Devices), and folder watching
- **Auto-discovery**: Finds compatible scanners on your network automatically
- **Paperless-NGX integration**: Direct API upload or consume folder drop
- **ADF auto-scan**: Automatically scans when paper is placed in the document feeder
- **Folder watcher**: Route files from any scanner that saves to a network share
- **Web UI**: Setup wizard + scan dashboard, works on phone and desktop
- **Three deployment options**: Windows EXE, Docker, or pip install

## Quick Start

### Option 1: Windows EXE (easiest)

1. Download `OpenScanHub.exe` from [Releases](https://github.com/parallaxintelligence/openscan-hub/releases)
2. Double-click to run
3. Your browser opens to the setup wizard
4. Follow the 6 steps to configure your scanner

### Option 2: Docker

```bash
docker compose up -d
```

Open `http://localhost:8020` in your browser.

For scanner auto-discovery to work, the container must use `network_mode: host`.

### Option 3: pip install

```bash
pip install openscan-hub
openscan
```

Open `http://localhost:8020` in your browser.

## Supported Scanners

### eSCL (AirScan) - Most consumer printers
HP, Canon, Epson, Brother, and most printers made after ~2015 that support AirPrint/AirScan.

### WSD (Web Services for Devices) - Enterprise scanners
Xerox, Ricoh, and enterprise MFPs that support WSD scanning.

### Folder Watch - Universal fallback
Any scanner that can save to a network folder. OpenScanHub watches the folder and routes files to your destinations.

## Configuration

Config is stored at:
- **Windows**: `%APPDATA%\OpenScanHub\config.json`
- **Docker**: `/config/openscan.json`
- **Linux/Mac**: `~/.config/openscan/config.json`

You can also set `OPENSCAN_CONFIG_DIR` environment variable to override.

## Paperless-NGX Integration

OpenScanHub supports two methods of sending scans to Paperless-NGX:

### Consume Folder (recommended for local installs)
Point OpenScanHub at your Paperless consume folder. New scans are dropped there and Paperless picks them up automatically.

### REST API (for remote Paperless instances)
Enter your Paperless URL and API token. Scans are uploaded directly via the API with optional tag assignment.

## Command Line Options

```
openscan [options]

Options:
  --port PORT        Web UI port (default: 8020)
  --no-browser       Don't auto-open browser on startup
  --log-level LEVEL  Logging level: DEBUG, INFO, WARNING, ERROR
  --version          Show version and exit
```

## Building from Source

### Windows EXE

```bash
pip install -e ".[tray,dev]"
python build_exe.py
# Output: dist/OpenScanHub.exe
```

### Docker

```bash
docker build -t openscan-hub .
```

## Project Structure

```
openscan/
  scanner/
    base.py          # Abstract scanner protocol interface
    escl.py          # eSCL (AirScan) implementation
    wsd.py           # WSD implementation
    folder_watch.py  # Folder watcher implementation
  web/
    server.py        # HTTP server + routing
    wizard_api.py    # Setup wizard endpoints
    dashboard_api.py # Dashboard endpoints
    static/          # HTML, CSS, JS
  config.py          # JSON config management
  discovery.py       # Unified scanner discovery
  output.py          # Output handlers (file, Paperless)
  monitor.py         # ADF state monitor
  main.py            # Entry point
  tray.py            # Windows system tray
```

## Dependencies

| Package | License | Purpose |
|---------|---------|---------|
| [zeroconf](https://pypi.org/project/zeroconf/) | Apache 2.0 | mDNS scanner discovery |
| [pystray](https://pypi.org/project/pystray/) | LGPL v3 | Windows system tray (optional) |
| [Pillow](https://pypi.org/project/Pillow/) | HPND | Tray icon image (optional) |

All other functionality uses Python standard library.

## License

MIT - see [LICENSE](LICENSE)
