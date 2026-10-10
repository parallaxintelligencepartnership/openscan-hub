# OpenScanHub add-on

OpenScanHub lets you put paper on your network scanner and have the scan
land in a folder. Paperless-NGX is optional.

This add-on is an optional way to run OpenScanHub. You do not need Home
Assistant to use it; see the main README for Windows and Docker installs.

## Install

1. In Home Assistant, open **Settings > Add-ons > Add-on Store**.
2. Open the menu (three dots, top right), choose **Repositories**, and add
   `https://github.com/parallaxintelligencepartnership/openscan-hub`
   (the whole repository URL, not the `homeassistant-addon` folder; the
   repository's `repository.yaml` is at its top level).
3. Find **OpenScanHub** in the store, click **Install**, then **Start**.
4. Turn on **Show in sidebar** and open **Scanner** from the sidebar.
5. Follow the three setup steps: pick your scanner, check the scans folder,
   and do a test scan.

## Options (Configuration tab)

| Option | Default | What it does |
|---|---|---|
| `scans_folder` | `/share/scans` | Where finished scans are saved. `/share` and `/media` are the Home Assistant shared folders, so you can reach scans over Samba or the Media browser. |
| `ftp_enabled` | `false` | Turns on the FTP receiver for scanners that only offer "Scan to FTP". |
| `ftp_port` | `2121` | Port for the FTP receiver. |

These two settings come from this tab. If you change the scans folder or the
FTP switch on the OpenScanHub settings page, the add-on options replace them
the next time the add-on starts.

## FTP receiver

When `ftp_enabled` is on, point your scanner's "Scan to FTP" at your Home
Assistant address, port 2121 (or the port you chose). Use the user name and
password shown on the OpenScanHub settings page. Anonymous login is off. Any
device on your network can reach this port, so leave it off if you do not
need it.

## Network

The add-on uses the host network so it can find scanners automatically and
receive FTP uploads. Home Assistant shows the web page through ingress, so
you do not need to open port 8020. Because of the host network, port 8020 on
your Home Assistant machine also answers other devices on your network, but
only after they sign in with the access token.

## Where things are stored

- Settings, the access token and the retry queue: the add-on's private
  `/data` folder (included in Home Assistant backups).
- Scans: the `scans_folder` option.

## Supported hardware

64-bit Intel or AMD (amd64) and 64-bit ARM (aarch64, for example a
Raspberry Pi 4 or 5 running the 64-bit OS).

## Help

Open an issue at
https://github.com/parallaxintelligencepartnership/openscan-hub/issues
and include the add-on log (Log tab). The log never contains your access
token or the FTP password. Logs from older versions of OpenScanHub printed
the access token at startup: if you ever posted one of those, use
**Regenerate token** in OpenScanHub's settings.
