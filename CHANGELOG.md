# Changelog

All notable changes to OpenScanHub.

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
