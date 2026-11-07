# Changelog

All notable changes to OpenScanHub.

## 0.4.0 (2026-11-06)

OpenScanHub is now built around auto-feed scanning: put paper in the feeder,
and it scans, cleans, reads, names and saves the file.

### Added

- Two editions from one codebase: Homelab and Office. Docker tags
  `:0.4.0`, `:latest` (Homelab) and `:0.4.0-office`, `:office` (Office).
  Windows files `OpenScanHub.exe` and `OpenScanHub-Office.exe`.
- Office edition: an admin token and a scan token, the audit log on by
  default, and no Home Assistant or Paperless-NGX code.
- Scan profiles: a profile editor, dashboard buttons, and a profile API.
  An old config gets one "Default" profile that follows the old Settings.
- Next feed profile: pick a profile on the dashboard or the phone page,
  then load the paper. The choice ends after one batch or 10 minutes.
- Phone page at `/m`: scanner and feeder status, the profile picker, and
  the last five scans. Installing it as an app needs HTTPS. Over plain HTTP
  it works in the browser.
- Processing: OCR to a searchable PDF, deskew, auto-rotate, crop, blank page
  removal, gray and black-and-white, compression, and PDF/A. OCR needs
  Tesseract. The Docker image has it. On Windows, you install it.
- Separator pages: split one batch at blank sheets or at `OSH:` QR sheets.
  Print a sheet from `/api/separator-sheet?label=`.
- Smart file names: `{doc_date}`, `{title}`, `{vendor}`, `{barcode}`,
  `{pages}`, and date formats such as `{doc_date:%Y-%m}`.
- Destinations: SMB share, WebDAV and Nextcloud, and email.
- Inputs: SMB share poll and an IMAP mailbox.
- Scan audit log (`audit.jsonl`).
- A retry queue that restarts delivery services when profiles change.
- Guides: Docker, Windows, Office, feeder to share, and a glossary.

### Changed

- Blank page removal now runs once, in the processing step. The scanner
  drivers no longer remove blank pages.
- Blank page removal now also applies to files that arrive by FTP or from a
  watched folder, when the option is on. Before, it did not.
- Every finished scan goes through one function, `intake.handle_scan`.
- Dependencies: pypdf stays on 6.x (6.21.4) and Pillow stays on 12.x
  (12.3.0). pypdf 7 and Pillow 13 are not part of 0.4.0.

### Notes

- A scan with no OCR still saves when Tesseract is missing. It carries a
  warning.
- An Office build ignores Home Assistant and Paperless-NGX settings in an old
  config file and keeps them in the file.

## 0.3.2 (2026-11-06)

Dependency refresh. No behavior changes.

- pypdf 6.21.4, zeroconf 0.152.1 (Python 3.10) and 0.154.0 (Python 3.11 and
  later), setuptools 84.2.1, pytest 9.2.0, pluggy 1.7.0,
  pyinstaller-hooks-contrib 2026.9.
- pypdf 7 and Pillow 13 are major releases. They wait for 0.4.0.

## 0.3.1 (2026-10-10)

Fixes found while deploying the Docker image.

- If the config folder is not writable (for example a bind-mounted `/config`
  owned by root), OpenScanHub now logs an error with the exact `chown`
  command, reports `config_writable: false` and `status: "degraded"` in
  `/api/health`, and shows a red warning on the dashboard and setup pages.
  It keeps running so you can see the message.
- `openscan --show-token` reads the token from the saved config file only.
  If there is no saved token yet, it says so and exits with code 1 instead of
  printing a new token that could never match the running server.
- The Docker image now has the `openscan` command, so
  `docker exec openscan-hub openscan --show-token` works.
  `python -m openscan` still works.
- `docker-compose.yml` explains why settings use a named volume and how to
  prepare a bind-mounted config folder.

## 0.3.0 (2026-10-10)

- Simpler setup: a three-step wizard and a dashboard, with a Settings page
  for everything else.
- Saves scans to a folder or NAS share first; Paperless-NGX and Home
  Assistant are optional.
- Web access is protected by an access token, a session cookie and Host and
  Origin checks.
- Docker image for amd64 and arm64: Python 3.13 slim, non-root user,
  hash-locked dependencies and a health check.
- Optional Home Assistant add-on with ingress.
- Failed deliveries are queued and retried.
- eSCL and WSD scanning fixes, multi-page flatbed scanning, and a safer FTP
  receiver.
- Replaced PyMuPDF with pypdfium2.
