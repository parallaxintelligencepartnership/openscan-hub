#!/bin/sh
# Start OpenScanHub inside Home Assistant.
set -eu

# Reachable by the Home Assistant ingress proxy; requests from the proxy
# address are trusted (Home Assistant already checked the user).
export OPENSCAN_BIND=0.0.0.0
export OPENSCAN_TRUST_PROXY_IP=172.30.32.2
export OPENSCAN_CONFIG_DIR=/data/openscan
export OPENSCAN_DOCKER=1

mkdir -p "$OPENSCAN_CONFIG_DIR"

# Apply the add-on options (Configuration tab) to the OpenScanHub config.
cd /app
python - <<'PY'
import json
from pathlib import Path

from openscan.config import load_config, save_config

opts = {}
try:
    opts = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
except (OSError, ValueError) as e:
    print(f"Could not read add-on options, using defaults: {e}")

cfg = load_config()
scans = str(opts.get("scans_folder") or "/share/scans")
Path(scans).mkdir(parents=True, exist_ok=True)
cfg.output.folder = scans
cfg.ftp_receive.enabled = bool(opts.get("ftp_enabled", False))
cfg.ftp_receive.port = int(opts.get("ftp_port", 2121))
save_config(cfg)
print(f"Scans folder: {scans}; FTP receiver: {'on' if cfg.ftp_receive.enabled else 'off'}")
PY

exec python -m openscan --no-browser
